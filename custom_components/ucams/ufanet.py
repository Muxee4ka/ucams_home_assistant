import asyncio
import logging
from urllib.parse import urljoin

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.ucams.utils import (
    CONF_DOM_URL,
    CONF_USERNAME,
    CONF_PASSWORD, TOKEN_REFRESH_BUFFER,
)

_LOGGER = logging.getLogger(__name__)


HEADERS = {
    "Connection": "Keep-Alive",
    "User-Agent": "okhttp/4.9.0",
}
BASE_URL = "https://dom.ufanet.ru/"


class DomApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self.username = config_entry.options[CONF_USERNAME]
        self.password = config_entry.options[CONF_PASSWORD]
        self.base_url = config_entry.options[CONF_DOM_URL]
        self.session = aiohttp.ClientSession(headers=HEADERS, trust_env=True)
        self.token = None
        self.token_expiration = 0

    async def _authenticate(self):
        url = urljoin(self.base_url, "api/v1/auth/auth_by_contract/")
        payload = {"contract": self.username, "password": self.password}
        async with self.session.post(url, json=payload, compress=False) as resp:
            if resp.status != 200:
                response_text = await resp.text()
                _LOGGER.error("Authentication failed: %s", response_text)
                raise ConfigEntryNotReady(f"Authentication failed: {response_text}")
            data = await resp.json()
            token = data["token"]
            access = token["access"]
            self.token_expiration = token.get("exp", 0)
            self.session.headers.update({"Authorization": f"JWT {access}"})

    async def get_authenticated_session(self):
        now = asyncio.get_running_loop().time()
        if not self.token or now >= self.token_expiration - TOKEN_REFRESH_BUFFER:
            await self._authenticate()
        return self.session

    async def get_shared_skud(self):
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/skud/shared/")
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def open_skud(self, skud_id):
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, f"api/v0/skud/shared/{skud_id}/open/")
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_contract_info(self):
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/contract/")
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_all_contracts(self):
        """Получение всех контрактов."""
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/contract_info/get_all_contract/")
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_contract_details(self, contract_id, billing_id):
        """Получение детальной информации о контракте."""
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/contract_info/get_contract_info/")
        payload = {"contracts": [{"contract_id": contract_id, "billing_id": billing_id}]}
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self):
        await self.session.close()
