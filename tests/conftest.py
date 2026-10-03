"""Plugin test fixtures and configuration for home_assistant."""

from unittest.mock import patch

import pytest
from src.plugins.testing import create_mock_response


@pytest.fixture(autouse=True)
def reset_plugin_singletons():
    """Reset plugin singletons before each test."""
    yield


@pytest.fixture(autouse=True)
def signed_out():
    """No test reaches the real OAuth service: by default nobody signed in.

    Tests that need a sign-in patch ``get_oauth_token`` themselves.
    """
    from plugins.home_assistant import HomeAssistantPlugin

    with patch.object(HomeAssistantPlugin, "get_oauth_token", return_value=None, create=True) as getter:
        yield getter


@pytest.fixture
def old_core():
    """A FiestaBoard core from before plugin sign-in: no ``get_oauth_token``."""
    from plugins.home_assistant import HomeAssistantPlugin

    with patch.object(HomeAssistantPlugin, "get_oauth_token", None, create=True), \
            patch.object(HomeAssistantPlugin, "report_oauth_rejected", None, create=True):
        yield


@pytest.fixture
def mock_api_response():
    """Fixture to create mock API responses."""
    return create_mock_response

