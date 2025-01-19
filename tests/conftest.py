# tests/conftest.py
import asyncio

import pytest
from aiohttp import ClientSession
from custom_components.ucams.ucams import UcamsApi
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


@pytest.fixture(scope="session")
def event_loop():
    """Создаёт цикл событий для тестов."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()

@pytest.fixture
async def hass(tmp_path):
    """Создаёт и инициализирует объект Home Assistant."""
    hass = HomeAssistant(config_dir=str(tmp_path))
    await hass.async_start()  # Запускаем HomeAssistant
    yield hass
    await hass.async_stop()  # Останавливаем HomeAssistant


@pytest.fixture
def config_entry():
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="ucams",
        title="Ucams",
        data={"name": "Test Config"},
        source="user",
        options={"camera_image_refresh_interval": 10},
        entry_id="1",
    )


@pytest.fixture
def mock_ufanet_api():
    class MockUfanetApi:
        def __init__(self):
            self.session = ClientSession()
            self.token_expiration = 0

        async def get_contract_info(self):
            return [{"isp_org": {"cams_server": {"url": "https://cams.example.com"}}}]

    return MockUfanetApi()


@pytest.fixture
def ucams_api(hass, config_entry, mock_ufanet_api):
    return UcamsApi(hass, config_entry, mock_ufanet_api)
