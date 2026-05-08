"""Common fixtures for ucams integration tests."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

if sys.platform == "win32":
    # aiohttp + aiodns refuses to start on Windows ProactorEventLoop. CI runs
    # on Linux so this only matters for local dev.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


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
    """Stubs the DomApi surface that UcamsApi consumes.

    Lets per-test code override get_cctv_list to inject a camera list
    without standing up a real aiohttp session.
    """

    class MockUfanetApi:
        def __init__(self):
            self.session = SimpleNamespace(headers={"Authorization": "JWT mock-dom-token"})
            # far-future expiration so cams_server auth path doesn't refuse
            self.token_expiration = 9_999_999_999
            self.cctv_payload: list[dict] = []

        async def get_contract_info(self):
            return [{"isp_org": {"cams_server": {"url": "https://cams.example.com"}}}]

        async def get_cctv_list(self):
            return self.cctv_payload

        async def get_authenticated_session(self):
            return self.session

    return MockUfanetApi()


async def _drain_aiohttp_shutdown_threads():
    """Wait for aiohttp's `_run_safe_shutdown_loop` daemons to exit.

    pytest-homeassistant-custom-component's lingering-thread guard fires
    immediately after teardown; on Linux+Python 3.12 the daemon outlives
    short waits, so we poll (3.13 cleans up faster). Skipped on Windows
    where the plugin behaves differently and polling can hang.
    """
    if sys.platform == "win32":
        return

    import threading

    deadline = 1.5
    elapsed = 0.0
    step = 0.05
    while elapsed < deadline:
        survivors = [t for t in threading.enumerate() if "_run_safe_shutdown_loop" in t.name]
        if not survivors:
            return
        await asyncio.sleep(step)
        elapsed += step


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
    await api.close()
    await _drain_aiohttp_shutdown_threads()


@pytest.fixture
async def dom_api(config_entry):
    from custom_components.ucams.ufanet import DomApi

    api = DomApi(MagicMock(spec=HomeAssistant), config_entry)
    yield api
    await api.close()
    await _drain_aiohttp_shutdown_threads()
