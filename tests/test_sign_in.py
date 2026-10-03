"""Sign in with Home Assistant (FiestaBoard 9.9.0 settings-based OAuth endpoints).

The pasted long-lived access token keeps working and wins when set. Sign-in is
used only when no token is pasted. MQTT Statestream is untouched.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from src.oauth.errors import ConnectionNotConfigured
from src.oauth.provider import parse_provider_block, validate_provider_block
from src.plugins.base import OptionsRequest

from plugins.home_assistant import HomeAssistantPlugin

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text())
BASE = "http://192.168.1.100:8123"
STATES = [{"entity_id": "sensor.temperature", "state": "72", "attributes": {"friendly_name": "Temp"}}]


def _plugin(**config):
    plugin = HomeAssistantPlugin(MANIFEST)
    plugin._config = {"base_url": BASE, "entities": [], **config}
    return plugin


def _ok(payload=None):
    response = Mock(status_code=200)
    response.json.return_value = STATES if payload is None else payload
    response.raise_for_status.return_value = None
    return response


def _unauthorized():
    import requests

    response = Mock(status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
    return response


def _bearer(call):
    return call.kwargs["headers"]["Authorization"]


# ── Manifest ────────────────────────────────────────────────────────────────


class TestManifestOAuthBlock:
    def test_block_is_valid_for_core(self):
        assert validate_provider_block(MANIFEST["oauth"], MANIFEST["settings_schema"]) == []

    def test_block_uses_ha_builtin_oauth_from_base_url(self):
        provider = parse_provider_block(MANIFEST["oauth"], MANIFEST["name"], MANIFEST["settings_schema"])
        assert provider.name == "Home Assistant"
        assert provider.flows == ("relay",)
        assert provider.endpoint_base_setting == "base_url"
        endpoints = provider.resolve_endpoints({"base_url": BASE + "/"})
        assert endpoints.authorization_url == BASE + "/auth/authorize"
        assert endpoints.token_url == BASE + "/auth/token"

    def test_client_id_is_relay_host_and_not_user_editable(self):
        """HA requires the client_id URL's host to equal the redirect host."""
        provider = parse_provider_block(MANIFEST["oauth"], MANIFEST["name"], MANIFEST["settings_schema"])
        assert provider.client_id == "https://fiestaboard.app/"
        assert provider.user_client_id is False
        assert provider.resolve_client_id({"client_id": "test_other"}) == "https://fiestaboard.app/"
        assert provider.resolve_client_secret({}) == ""
        assert "client_secret" not in json.dumps(MANIFEST["oauth"])

    @pytest.mark.parametrize(
        "base",
        ["https://example.ui.nabu.casa", "http://homeassistant.local:8123", "http://homeassistant:8123"],
    )
    def test_https_or_home_network_addresses_resolve(self, base):
        provider = parse_provider_block(MANIFEST["oauth"], MANIFEST["name"], MANIFEST["settings_schema"])
        assert provider.resolve_endpoints({"base_url": base}).token_url == base + "/auth/token"

    @pytest.mark.parametrize("base", ["", "http://ha.example.com", "http://user:pw@192.168.1.5:8123"])
    def test_unsafe_or_missing_address_refuses_sign_in(self, base):
        provider = parse_provider_block(MANIFEST["oauth"], MANIFEST["name"], MANIFEST["settings_schema"])
        with pytest.raises(ConnectionNotConfigured):
            provider.resolve_endpoints({"base_url": base})

    def test_requires_core_with_settings_endpoints(self):
        assert MANIFEST["fiestaboard_version"] == ">=9.9.0"

    def test_minor_version_bump(self):
        assert MANIFEST["version"] == "1.5.0"

    def test_existing_settings_keys_kept(self):
        props = MANIFEST["settings_schema"]["properties"]
        for key in ("base_url", "access_token", "entities", "timeout", "mqtt_statestream", "statestream_base_topic"):
            assert key in props
        assert props["access_token"]["ui:widget"] == "password"


# ── Which token is used ─────────────────────────────────────────────────────


class TestTokenChoice:
    @patch("plugins.home_assistant.requests.get")
    def test_pasted_token_wins_over_sign_in(self, mock_get, signed_out):
        signed_out.return_value = "test_oauth_token"
        mock_get.return_value = _ok()
        result = _plugin(access_token="test_pasted").fetch_data()
        assert result.available
        assert all(_bearer(c) == "Bearer test_pasted" for c in mock_get.call_args_list)
        signed_out.assert_not_called()

    @patch("plugins.home_assistant.requests.get")
    def test_sign_in_used_when_no_token_pasted(self, mock_get, signed_out):
        signed_out.return_value = "test_oauth_token"
        mock_get.return_value = _ok()
        result = _plugin(access_token="").fetch_data()
        assert result.available
        assert result.data["entity_count"] == 1
        assert all(_bearer(c) == "Bearer test_oauth_token" for c in mock_get.call_args_list)

    @patch("plugins.home_assistant.requests.get")
    def test_blank_pasted_token_falls_through_to_sign_in(self, mock_get, signed_out):
        signed_out.return_value = "test_oauth_token"
        mock_get.return_value = _ok()
        assert _plugin(access_token="   ").fetch_data().available
        assert _bearer(mock_get.call_args) == "Bearer test_oauth_token"

    @patch("plugins.home_assistant.requests.get")
    def test_neither_token_nor_sign_in_makes_no_request(self, mock_get):
        result = _plugin().fetch_data()
        assert not result.available
        assert "Home Assistant not configured" in result.error
        assert "sign in" in result.error.lower()
        mock_get.assert_not_called()

    @patch("plugins.home_assistant.requests.get")
    def test_sign_in_lookup_error_is_treated_as_signed_out(self, mock_get, signed_out):
        signed_out.side_effect = RuntimeError("boom")
        result = _plugin().fetch_data()
        assert not result.available
        mock_get.assert_not_called()

    @pytest.mark.usefixtures("old_core")
    @patch("plugins.home_assistant.requests.get")
    def test_old_core_still_uses_pasted_token(self, mock_get):
        mock_get.return_value = _ok()
        assert _plugin(access_token="test_pasted").fetch_data().available
        assert _bearer(mock_get.call_args) == "Bearer test_pasted"


# ── A token Home Assistant refuses ──────────────────────────────────────────


class TestRejectedSignIn:
    @patch("plugins.home_assistant.requests.get")
    def test_401_on_sign_in_reports_and_retries_with_new_token(self, mock_get, signed_out):
        signed_out.return_value = "test_old"
        mock_get.side_effect = [_unauthorized(), _ok({"message": "API running."}), _ok()]
        plugin = _plugin()

        def refreshed():  # core force-refreshes and stores the new token
            signed_out.return_value = "test_new"
            return "test_new"

        with patch.object(HomeAssistantPlugin, "report_oauth_rejected", side_effect=refreshed, create=True) as rep:
            result = plugin.fetch_data()
        assert result.available
        rep.assert_called_once()
        assert _bearer(mock_get.call_args_list[1]) == "Bearer test_new"
        assert _bearer(mock_get.call_args_list[2]) == "Bearer test_new"

    @patch("plugins.home_assistant.requests.get")
    def test_401_on_sign_in_without_new_token_asks_to_sign_in_again(self, mock_get, signed_out):
        signed_out.return_value = "test_old"
        mock_get.return_value = _unauthorized()
        with patch.object(HomeAssistantPlugin, "report_oauth_rejected", return_value=None, create=True) as rep:
            result = _plugin().fetch_data()
        assert not result.available
        assert "sign in" in result.error.lower()
        rep.assert_called_once()
        assert mock_get.call_count == 1

    @patch("plugins.home_assistant.requests.get")
    def test_401_on_pasted_token_never_reports_sign_in(self, mock_get, signed_out):
        mock_get.return_value = _unauthorized()
        with patch.object(HomeAssistantPlugin, "report_oauth_rejected", create=True) as rep:
            result = _plugin(access_token="test_pasted").fetch_data()
        assert not result.available
        # Same message as before sign-in existed: no sign-in hint for pasted tokens.
        assert result.error == "Failed to connect to Home Assistant"
        rep.assert_not_called()

    @patch("plugins.home_assistant.requests.get")
    def test_401_on_core_without_report_hook(self, mock_get, signed_out):
        signed_out.return_value = "test_old"
        mock_get.return_value = _unauthorized()
        with patch.object(HomeAssistantPlugin, "report_oauth_rejected", None, create=True):
            result = _plugin().fetch_data()
        assert not result.available
        assert mock_get.call_count == 1

    @patch("plugins.home_assistant.requests.get")
    def test_report_hook_error_does_not_crash(self, mock_get, signed_out):
        signed_out.return_value = "test_old"
        mock_get.return_value = _unauthorized()
        with patch.object(HomeAssistantPlugin, "report_oauth_rejected", side_effect=RuntimeError("x"), create=True):
            result = _plugin().fetch_data()
        assert not result.available


# ── Validation, options, MQTT ───────────────────────────────────────────────


class TestSetupWithSignIn:
    def test_url_without_token_is_valid_when_core_supports_sign_in(self):
        assert _plugin().validate_config({"base_url": BASE}) == []

    def test_url_still_required_for_rest(self):
        assert "Home Assistant URL is required" in _plugin().validate_config({})

    @pytest.mark.usefixtures("old_core")
    def test_old_core_still_requires_token(self):
        assert "Access token is required" in _plugin().validate_config({"base_url": BASE})

    @patch("plugins.home_assistant.requests.get")
    def test_entity_picker_browses_with_sign_in(self, mock_get, signed_out):
        signed_out.return_value = "test_oauth_token"
        mock_get.return_value = _ok()
        result = _plugin().get_options(OptionsRequest(options_id="entities"))
        assert [o.value for o in result.options] == ["sensor.temperature"]
        assert _bearer(mock_get.call_args) == "Bearer test_oauth_token"

    def test_entity_picker_hint_mentions_sign_in(self):
        from src.plugins.base import OptionsUnavailable

        with pytest.raises(OptionsUnavailable) as exc:
            _plugin(base_url="").get_options(OptionsRequest(options_id="entities"))
        message = str(exc.value).lower()
        assert "url" in message and "token" in message and "sign in" in message

    @patch("plugins.home_assistant.requests.get")
    @patch("plugins.home_assistant.mqtt_listener.HAStateStreamListener")
    def test_mqtt_fallback_to_rest_uses_sign_in(self, MockListener, mock_get, signed_out):
        signed_out.return_value = "test_oauth_token"
        MockListener.return_value.is_connected.return_value = False
        mock_get.return_value = _ok()
        result = _plugin(mqtt_statestream=True).fetch_data()
        assert result.available
        assert result.data["data_source"] == "rest"
        assert _bearer(mock_get.call_args) == "Bearer test_oauth_token"


@pytest.mark.usefixtures("old_core")
@patch("plugins.home_assistant.requests.get")
def test_old_core_without_token_is_not_configured(mock_get):
    result = _plugin().fetch_data()
    assert not result.available
    mock_get.assert_not_called()
