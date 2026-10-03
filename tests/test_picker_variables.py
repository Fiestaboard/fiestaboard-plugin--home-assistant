"""Configured entities are listed in the template editor's variable picker (issue #14).

The picker shows a plugin's declared variables plus, when ``auto_discover`` is
on, every top-level *scalar* key its data returns. Entity data is a dict keyed
by the dotted entity_id, so on its own it never reaches the picker. Each entity
under "Entities to Monitor" therefore also gets a flat ``<domain>_<object_id>``
key holding its state, which ``{{home_assistant.<domain>_<object_id>}}`` reads.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from plugins.home_assistant import HomeAssistantPlugin
from src.plugins.manifest import PluginManifest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def _manifest():
    return json.loads(MANIFEST_PATH.read_text())


def _rest_responses(states):
    def respond(url, **kwargs):
        resp = Mock()
        resp.raise_for_status = Mock()
        if url.endswith("/api/"):
            resp.json.return_value = {"message": "API running."}
        else:
            resp.json.return_value = states
        return resp

    return respond


STATES = [
    {"entity_id": "sensor.living_room_temperature", "state": "72", "attributes": {"friendly_name": "Temp"}},
    {"entity_id": "binary_sensor.front_door", "state": "off", "attributes": {}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]

CONFIGURED = [
    {"entity_id": "sensor.living_room_temperature", "name": "Temp"},
    {"entity_id": "binary_sensor.front_door", "name": "Door"},
]


def _rest_plugin(entities=CONFIGURED):
    plugin = HomeAssistantPlugin(_manifest())
    plugin._config = {"base_url": "http://ha.local:8123", "access_token": "test_token", "entities": entities}
    return plugin


class TestConfiguredEntityKeysRest:
    @patch("plugins.home_assistant.requests.get")
    def test_configured_entity_state_is_a_flat_key(self, mock_get):
        mock_get.side_effect = _rest_responses(STATES)
        data = _rest_plugin().fetch_data().data
        assert data["sensor_living_room_temperature"] == "72"
        assert data["binary_sensor_front_door"] == "off"

    @patch("plugins.home_assistant.requests.get")
    def test_unconfigured_entity_gets_no_flat_key(self, mock_get):
        mock_get.side_effect = _rest_responses(STATES)
        data = _rest_plugin().fetch_data().data
        assert "light_kitchen" not in data
        # Still reachable through the dotted form the engine resolves.
        assert data["light.kitchen"]["state"] == "on"

    @patch("plugins.home_assistant.requests.get")
    def test_configured_entity_missing_from_home_assistant_gets_no_flat_key(self, mock_get):
        mock_get.side_effect = _rest_responses(STATES)
        data = _rest_plugin([{"entity_id": "sensor.gone", "name": "Gone"}]).fetch_data().data
        assert "sensor_gone" not in data

    @patch("plugins.home_assistant.requests.get")
    def test_flat_keys_are_scalars_core_discovery_picks_up(self, mock_get):
        """Core's auto-discovery skips dicts and lists; the flat keys must be plain strings."""
        mock_get.side_effect = _rest_responses(STATES)
        data = _rest_plugin().fetch_data().data
        for key in ("sensor_living_room_temperature", "binary_sensor_front_door"):
            assert isinstance(data[key], str)
            assert "." not in key


class TestConfiguredEntityKeysMqtt:
    @patch("plugins.home_assistant.mqtt_listener.HAStateStreamListener")
    def test_configured_entity_state_is_a_flat_key(self, MockListener):
        listener = MagicMock()
        listener.is_connected.return_value = True
        listener.get_entities.return_value = {
            "sensor.living_room_temperature": {"state": "72", "attributes": {}, "friendly_name": "Temp"},
            "light.kitchen": {"state": "on", "attributes": {}, "friendly_name": "Kitchen"},
        }
        MockListener.return_value = listener

        plugin = HomeAssistantPlugin(_manifest())
        plugin._config = {
            "mqtt_statestream": True,
            "statestream_broker_host": "mqtt.local",
            "entities": [{"entity_id": "sensor.living_room_temperature", "name": "Temp"}],
        }
        data = plugin.fetch_data().data

        assert data["sensor_living_room_temperature"] == "72"
        assert "light_kitchen" not in data


class TestManifestEnablesDiscovery:
    def test_core_parses_auto_discover_as_enabled(self):
        """Declared variables turn auto_discover off by default, so it must be explicit."""
        assert PluginManifest.from_dict(_manifest()).variables.auto_discover is True
