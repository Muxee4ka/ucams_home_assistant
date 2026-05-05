"""Common fixtures for ucams integration tests."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant


@pytest.fixture
def config_entry():
    return SimpleNamespace(
        version=1,
        minor_version=1,
        domain="ucams",
        title="Ucams",
        data={"name": "Test Config"},
        source="user",
        options={
            "camera_image_refresh_interval": 10,
            "username": "12345",
            "password": "secret",
            "dom_link": "https://dom.example.com",
        },
        entry_id="1",
    )


@pytest.fixture
def mock_ufanet_api():
    class MockUfanetApi:
        def __init__(self):
            self.session = SimpleNamespace(headers={})
            self.token_expiration = 0

        async def get_contract_info(self):
            return [{"isp_org": {"cams_server": {"url": "https://cams.example.com"}}}]

    return MockUfanetApi()


@pytest.fixture
async def ucams_api(config_entry, mock_ufanet_api):
    """A UcamsApi instance with a stubbed-out hass.

    UcamsApi only stores hass on the instance — it never calls into it — so a
    MagicMock is enough and lets these stay pure unit tests, no event loop or
    HomeAssistant boot required.
    """
    from custom_components.ucams.ucams import UcamsApi

    api = UcamsApi(MagicMock(spec=HomeAssistant), config_entry, mock_ufanet_api)
    yield api
    await api.session.close()


@pytest.fixture
async def dom_api(config_entry):
    from custom_components.ucams.ufanet import DomApi

    api = DomApi(MagicMock(spec=HomeAssistant), config_entry)
    yield api
    await api.close()
