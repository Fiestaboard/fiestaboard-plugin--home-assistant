"""Tests for the home_assistant ``entities`` remote-options provider.

The settings form's Entity field is a ``remote-options`` picker backed by
``get_options()``.  These tests pin the behaviour that makes the picker useful:
it browses the *whole* Home Assistant catalog, not the handful of entities the
user has already configured.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from plugins.home_assistant import HomeAssistantPlugin
from src.plugins.base import Option, OptionsRequest, OptionsResult, OptionsUnavailable

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def _ha_manifest():
    """A minimal manifest good enough to construct the plugin."""
    return {
        "id": "home_assistant",
        "name": "Home Assistant",
        "version": "1.0.0",
        "description": "Display entity states",
        "author": "Test",
        "settings_schema": {},
        "variables": {},
    }


# The catalog Home Assistant reports.  Deliberately *larger* than the
# configured list below: a picker exists to show you what you have not picked.
FULL_CATALOG = [
    {
        "entity_id": "sensor.temperature",
        "state": "72.5",
        "attributes": {"friendly_name": "Living Room Temperature"},
    },
    {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {"friendly_name": "Living Room Lamp"},
    },
    {
        "entity_id": "binary_sensor.front_door",
        "state": "off",
        "attributes": {"friendly_name": "Front Door"},
    },
    {
        "entity_id": "lock.garage",
        "state": "locked",
        "attributes": {},
    },
]

# What the user has already configured — one entity out of the four above.
CONFIGURED_ENTITIES = [{"entity_id": "sensor.temperature", "name": "Temp"}]


def _plugin(**overrides):
    """Build a plugin whose stored config knows about one entity only."""
    config = {
        "base_url": "http://ha.local:8123",
        "access_token": "test_token",
        "entities": list(CONFIGURED_ENTITIES),
    }
    config.update(overrides)
    plugin = HomeAssistantPlugin(_ha_manifest())
    plugin.config = config
    return plugin


def _rest_response(catalog=None):
    """A stand-in for ``requests.get`` returning the HA /states payload."""
    response = Mock()
    response.json.return_value = FULL_CATALOG if catalog is None else catalog
    response.raise_for_status.return_value = None
    return response


class TestEntitiesOptionsCatalog:
    """The provider browses the whole catalog, not the configured subset."""

    def test_returns_full_catalog_not_just_configured_entities(self):
        """get_options must offer entities the user has NOT configured yet.

        This is the entire point of the picker.  ``fetch_data`` intersects the
        catalog with ``config['entities']``; reusing that path here would offer
        the user only the entity they already have.
        """
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        values = [option.value for option in result.options]
        assert values == [
            "binary_sensor.front_door",
            "light.living_room",
            "lock.garage",
            "sensor.temperature",
        ], "picker must offer the whole catalog, not only configured entities"

    def test_option_carries_friendly_name_and_state_but_not_attributes(self):
        """Each option is id + friendly name + a short state preview.

        The payload stays small on purpose: the catalog can run to thousands of
        entities and every attribute of every one of them is megabytes.
        """
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        by_value = {option.value: option for option in result.options}

        temperature = by_value["sensor.temperature"]
        assert temperature.label == "sensor.temperature"
        assert temperature.description == "Living Room Temperature"
        assert temperature.preview == "72.5"

        # No friendly_name upstream, so it would only echo the id — drop it.
        assert by_value["lock.garage"].description is None
        assert by_value["lock.garage"].preview == "locked"

    def test_preview_is_truncated_to_forty_characters(self):
        """A pathological state must not blow up the picker row."""
        catalog = [
            {
                "entity_id": "sensor.essay",
                "state": "x" * 200,
                "attributes": {},
            }
        ]
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response(catalog)):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        assert result.options[0].preview == "x" * 40


class TestEntitiesOptionsQuery:
    """``server_search`` sends the user's keystrokes down as ``request.query``."""

    def _search(self, query):
        plugin = _plugin()
        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities", query=query))
        return [option.value for option in result.options]

    def test_query_matches_entity_id_case_insensitively(self):
        """Typing 'LIGHT' finds light.living_room."""
        assert self._search("LIGHT") == ["light.living_room"]

    def test_query_matches_friendly_name_case_insensitively(self):
        """'front door' is only in the friendly name, not the id's spelling."""
        assert self._search("front door") == ["binary_sensor.front_door"]

    def test_query_matching_neither_returns_nothing(self):
        assert self._search("nonexistent") == []

    def test_empty_query_returns_everything(self):
        assert len(self._search("")) == len(FULL_CATALOG)


class TestEntitiesOptionsPaging:
    """``limit`` bounds the page; ``total``/``has_more`` describe the rest."""

    def _fetch(self, **kwargs):
        plugin = _plugin()
        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            return plugin.get_options(OptionsRequest(options_id="entities", **kwargs))

    def test_limit_caps_the_number_of_options(self):
        result = self._fetch(limit=2)
        assert [option.value for option in result.options] == [
            "binary_sensor.front_door",
            "light.living_room",
        ]

    def test_limit_sets_has_more_and_total_from_the_full_match_set(self):
        """``total`` is how many matched, not how many fitted on the page."""
        result = self._fetch(limit=2)
        assert result.has_more is True
        assert result.total == 4

    def test_no_has_more_when_the_page_holds_every_match(self):
        result = self._fetch(limit=100)
        assert result.has_more is False
        assert result.total == 4

    def test_total_counts_matches_not_the_whole_catalog(self):
        """A query narrows the catalog before the page is cut."""
        result = self._fetch(query="living", limit=1)
        assert result.total == 2
        assert result.has_more is True


class TestEntitiesOptionsUnconfigured:
    """Browsing happens *during* setup, so an empty config is normal."""

    def test_no_credentials_and_no_statestream_raises_options_unavailable(self):
        """Core turns OptionsUnavailable into an inline hint, not a 502."""
        plugin = _plugin(base_url="", access_token="", entities=[])

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="entities"))

    def test_unconfigured_message_names_what_is_missing(self):
        plugin = _plugin(base_url="", access_token="", entities=[])

        with pytest.raises(OptionsUnavailable) as excinfo:
            plugin.get_options(OptionsRequest(options_id="entities"))

        message = str(excinfo.value).lower()
        assert "url" in message and "token" in message

    def test_unconfigured_never_reaches_the_network(self):
        """A missing base_url would otherwise be requested as '/api/states'."""
        plugin = _plugin(base_url="", access_token="", entities=[])

        with patch("plugins.home_assistant.requests.get") as mock_get:
            with pytest.raises(OptionsUnavailable):
                plugin.get_options(OptionsRequest(options_id="entities"))

        mock_get.assert_not_called()

    def test_token_without_url_is_still_unavailable(self):
        plugin = _plugin(base_url="", entities=[])

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="entities"))

    def test_url_without_token_is_still_unavailable(self):
        plugin = _plugin(access_token="", entities=[])

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="entities"))


# The statestream listener's in-memory map, distinguishable from the REST
# catalog so a test can tell which source answered.
STATESTREAM_ENTITIES = {
    "switch.kettle": {"state": "off", "friendly_name": "Kettle", "attributes": {}},
    "sensor.humidity": {"state": "48", "friendly_name": "Humidity", "attributes": {}},
}


def _connected_listener(entities=None):
    listener = Mock()
    listener.is_connected.return_value = True
    listener.get_entities.return_value = (
        dict(STATESTREAM_ENTITIES) if entities is None else entities
    )
    return listener


class TestEntitiesOptionsStatestream:
    """When statestream is live it already holds the catalog — use it."""

    def test_connected_statestream_supplies_the_catalog(self):
        plugin = _plugin(mqtt_statestream=True)
        plugin._mqtt_listener = _connected_listener()

        with patch("plugins.home_assistant.requests.get") as mock_get:
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        assert [option.value for option in result.options] == [
            "sensor.humidity",
            "switch.kettle",
        ]
        mock_get.assert_not_called()

    def test_statestream_wins_over_rest_when_both_are_available(self):
        """Same decision fetch_data makes: the live map beats a poll."""
        plugin = _plugin(mqtt_statestream=True)
        plugin._mqtt_listener = _connected_listener()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        values = [option.value for option in result.options]
        assert "switch.kettle" in values
        assert "sensor.temperature" not in values

    def test_statestream_entities_keep_friendly_name_and_state(self):
        plugin = _plugin(mqtt_statestream=True)
        plugin._mqtt_listener = _connected_listener()

        result = plugin.get_options(OptionsRequest(options_id="entities"))
        kettle = {option.value: option for option in result.options}["switch.kettle"]

        assert kettle.description == "Kettle"
        assert kettle.preview == "off"

    def test_statestream_catalog_is_searchable_like_the_rest_one(self):
        plugin = _plugin(mqtt_statestream=True)
        plugin._mqtt_listener = _connected_listener()

        result = plugin.get_options(OptionsRequest(options_id="entities", query="KETTLE"))

        assert [option.value for option in result.options] == ["switch.kettle"]

    def test_disconnected_statestream_falls_back_to_rest(self):
        plugin = _plugin(mqtt_statestream=True)
        listener = Mock()
        listener.is_connected.return_value = False
        plugin._mqtt_listener = listener

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        assert "sensor.temperature" in [option.value for option in result.options]

    def test_statestream_only_setup_needs_no_rest_credentials(self):
        """Statestream users may never enter a URL or token at all."""
        plugin = _plugin(base_url="", access_token="", mqtt_statestream=True, entities=[])
        plugin._mqtt_listener = _connected_listener()

        result = plugin.get_options(OptionsRequest(options_id="entities"))

        assert [option.value for option in result.options] == [
            "sensor.humidity",
            "switch.kettle",
        ]

    def test_statestream_enabled_but_never_connected_and_no_rest_is_unavailable(self):
        plugin = _plugin(base_url="", access_token="", mqtt_statestream=True, entities=[])

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="entities"))

    def test_unavailable_message_mentions_statestream_for_statestream_users(self):
        """Telling a statestream user to paste a token is unhelpful advice."""
        plugin = _plugin(base_url="", access_token="", mqtt_statestream=True, entities=[])

        with pytest.raises(OptionsUnavailable) as excinfo:
            plugin.get_options(OptionsRequest(options_id="entities"))

        assert "statestream" in str(excinfo.value).lower()

    def test_get_options_does_not_start_a_listener(self):
        """get_options runs on a throwaway instance; it must not open sockets."""
        plugin = _plugin(base_url="", access_token="", mqtt_statestream=True, entities=[])

        with patch.object(HomeAssistantPlugin, "_ensure_mqtt_listener") as ensure:
            with pytest.raises(OptionsUnavailable):
                plugin.get_options(OptionsRequest(options_id="entities"))

        ensure.assert_not_called()


class TestEntitiesOptionsUpstreamDown:
    """"Configured but unreachable" is also an ask-me-later, not a bug."""

    def test_unreachable_home_assistant_raises_options_unavailable(self):
        """_fetch_all_entities swallows errors into {}; an empty picker with no
        explanation looks like "you have no entities", which is a lie."""
        plugin = _plugin()

        with patch(
            "plugins.home_assistant.requests.get",
            side_effect=requests.ConnectionError("refused"),
        ):
            with pytest.raises(OptionsUnavailable) as excinfo:
                plugin.get_options(OptionsRequest(options_id="entities"))

        assert "home assistant" in str(excinfo.value).lower()

    def test_a_query_that_matches_nothing_is_not_treated_as_unreachable(self):
        """An empty *result* is fine; an empty *catalog* is what's suspicious."""
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(
                OptionsRequest(options_id="entities", query="no-such-entity")
            )

        assert result.options == []
        assert result.total == 0


class TestUnknownOptionsId:
    """One method serves every provider, so it must reject the ones it lacks."""

    def test_unknown_options_id_raises_not_implemented_error(self):
        """Core maps NotImplementedError to 501 — 'no such provider here'."""
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            with pytest.raises(NotImplementedError):
                plugin.get_options(OptionsRequest(options_id="areas"))

    def test_unknown_options_id_names_the_provider_asked_for(self):
        plugin = _plugin()

        with pytest.raises(NotImplementedError) as excinfo:
            plugin.get_options(OptionsRequest(options_id="areas"))

        assert "areas" in str(excinfo.value)

    def test_unknown_options_id_is_rejected_before_any_upstream_call(self):
        """Don't poll Home Assistant to answer a question we can't answer."""
        plugin = _plugin()

        with patch("plugins.home_assistant.requests.get") as mock_get:
            with pytest.raises(NotImplementedError):
                plugin.get_options(OptionsRequest(options_id="areas"))

        mock_get.assert_not_called()

    def test_unknown_options_id_beats_being_unconfigured(self):
        """A provider that does not exist is a bug, not an ask-me-later."""
        plugin = _plugin(base_url="", access_token="", entities=[])

        with pytest.raises(NotImplementedError):
            plugin.get_options(OptionsRequest(options_id="areas"))


def _manifest():
    with open(MANIFEST_PATH) as handle:
        return json.load(handle)


def _entity_id_property():
    return _manifest()["settings_schema"]["properties"]["entities"]["items"]["properties"][
        "entity_id"
    ]


class TestManifestDeclaresThePicker:
    """The manifest half of the contract, checked against core itself."""

    def test_entity_id_is_a_remote_options_field(self):
        assert _entity_id_property()["ui:widget"] == "remote-options"

    def test_entity_id_points_at_the_entities_provider(self):
        """The options_id must be the one get_options actually answers."""
        assert _entity_id_property()["ui:options"]["options_id"] == "entities"

    def test_declared_options_ids_are_all_implemented(self):
        """Every provider the manifest promises must answer, not raise 501."""
        from src.plugins.manifest import collect_options_ids

        plugin = _plugin()
        for options_id in collect_options_ids(_manifest()["settings_schema"]):
            with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
                plugin.get_options(OptionsRequest(options_id=options_id))

    def test_ui_options_passes_cores_settings_schema_validator(self):
        """core rejects unknown/ill-typed ui:options keys, and load_manifest()
        returns None on any error — a typo here means the plugin will not load."""
        from src.plugins.manifest import validate_settings_schema_ui

        assert validate_settings_schema_ui(_manifest()["settings_schema"]) == []

    def test_whole_manifest_passes_cores_validator(self):
        from src.plugins.manifest import validate_manifest

        is_valid, errors = validate_manifest(_manifest())
        assert is_valid, errors

    def test_server_search_is_enabled_so_query_reaches_the_plugin(self):
        """get_options honours request.query; without this flag it never gets one."""
        assert _entity_id_property()["ui:options"]["server_search"] is True


# Config exactly as already stored on disk by users running 1.2.3, before the
# picker existed and every entity id was hand-typed.
STORED_CONFIG_V1 = {
    "enabled": True,
    "base_url": "http://ha.local:8123",
    "access_token": "test_token",
    "entities": [
        {"entity_id": "sensor.temperature", "name": "Temp"},
        {"entity_id": "light.living_room", "name": "Lights"},
    ],
    "timeout": 5,
    "refresh_seconds": 30,
}


class TestStoredConfigBackwardCompatibility:
    """The widget goes on the leaf, so the stored shape must not move.

    The tempting wrong migration is to hang ``remote-options`` off the
    ``entities`` array itself with ``multiple: true``.  That turns the setting
    into an array of bare strings and silently invalidates every config in the
    field.  These tests fail if that happens.
    """

    def test_existing_config_validates_against_the_new_schema(self):
        import jsonschema

        jsonschema.validate(STORED_CONFIG_V1, _manifest()["settings_schema"])

    def test_entities_is_still_an_array_of_objects(self):
        entities = _manifest()["settings_schema"]["properties"]["entities"]
        assert entities["type"] == "array"
        assert entities["items"]["type"] == "object"

    def test_entity_id_is_still_a_plain_string_property(self):
        """A remote-options leaf stores the scalar Option.value, unchanged."""
        assert _entity_id_property()["type"] == "string"

    def test_entity_id_and_name_are_both_still_required(self):
        items = _manifest()["settings_schema"]["properties"]["entities"]["items"]
        assert sorted(items["required"]) == ["entity_id", "name"]

    def test_the_widget_is_not_on_the_entities_array_itself(self):
        entities = _manifest()["settings_schema"]["properties"]["entities"]
        assert "ui:widget" not in entities
        assert "multiple" not in entities.get("ui:options", {})

    def test_an_array_of_bare_strings_is_rejected(self):
        """Proof the schema did not quietly loosen into a multi-select."""
        import jsonschema

        loosened = dict(STORED_CONFIG_V1, entities=["sensor.temperature"])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(loosened, _manifest()["settings_schema"])

    def test_stored_config_still_round_trips_through_fetch_data(self):
        """The names users chose still key the board data after the migration."""
        plugin = HomeAssistantPlugin(_ha_manifest())
        plugin.config = dict(STORED_CONFIG_V1)

        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.fetch_data()

        assert result.available
        assert result.data["entities"] == {
            "Temp": {
                "entity_id": "sensor.temperature",
                "state": "72.5",
                "friendly_name": "Living Room Temperature",
            },
            "Lights": {
                "entity_id": "light.living_room",
                "state": "on",
                "friendly_name": "Living Room Lamp",
            },
        }

    def test_stored_config_still_passes_validate_config(self):
        plugin = HomeAssistantPlugin(_ha_manifest())
        assert plugin.validate_config(STORED_CONFIG_V1) == []

    def test_a_value_the_picker_produces_is_a_valid_entity_id(self):
        """An Option.value dropped straight into stored config still validates."""
        import jsonschema

        plugin = _plugin()
        with patch("plugins.home_assistant.requests.get", return_value=_rest_response()):
            result = plugin.get_options(OptionsRequest(options_id="entities"))

        picked = dict(
            STORED_CONFIG_V1,
            entities=[{"entity_id": result.options[0].value, "name": "New"}],
        )
        jsonschema.validate(picked, _manifest()["settings_schema"])


class TestCoreVersionFloor:
    """The picker is not optional polish — it needs core to understand it."""

    # 8.24.2 is the release whose manifest validator accepts the widget's full
    # ui:options grammar. On anything older, `searchable` and `server_search`
    # are "unknown ui:options key" errors, and load_manifest() returns None on
    # any validation error — so the plugin would not load at all.
    REQUIRED_FLOOR = (8, 24, 2)

    def test_floor_is_at_least_the_release_that_accepts_this_manifest(self):
        spec = _manifest()["fiestaboard_version"]
        assert spec.startswith(">="), spec

        floor = tuple(int(part) for part in spec[2:].strip().split("."))
        assert floor >= self.REQUIRED_FLOOR, (
            f"{spec} lets the plugin install on a core that rejects its own "
            f"manifest; needs >= {'.'.join(map(str, self.REQUIRED_FLOOR))}"
        )
