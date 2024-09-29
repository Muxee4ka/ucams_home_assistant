import asyncio
import datetime
import logging
from functools import partial
from os.path import join


import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from requests import Response

from custom_components.ucams.utils import (
    CONF_DOM_URL,
    CONF_USERNAME,
    CONF_PASSWORD,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

HEADERS = {
    "Accept-Language": "ru_RU",
    "Content-Type": "application/json",
    "Content-Length": "50",
    "Host": "dom.ufanet.ru",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
    "User-Agent": "okhttp/4.9.0",
}
BASE_URL = "https://dom.ufanet.ru/"


class DomApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self.username = config_entry.options[CONF_USERNAME]
        self.password = config_entry.options[CONF_PASSWORD]
        self.base_url = config_entry.options[CONF_DOM_URL]
        self.config_entry_name = "DomUfanet"
        self.lock = asyncio.Lock()
        self._session = None
        self._expiration_date = 0
        self._access = None

    @property
    async def session(self):
        if (
            not self._session
            or datetime.datetime.now().timestamp() >= self._expiration_date
        ):
            self._session = requests.Session()
            data = {
                "contract": self.username,
                "password": self.password,
            }
            url = join(self.base_url, "api/v1/auth/auth_by_contract/")
            async with self.lock:
                res: Response = await self.hass.async_add_executor_job(
                    partial(self._session.post, url, headers=HEADERS, json=data)
                )
                res.raise_for_status()
                token_info = res.json()["token"]
                self._expiration_date = token_info["exp"]
                self._access = token_info["access"]
        return self._session

    async def get_session(self):
        session = await self.session
        headers = HEADERS.copy()
        headers.pop("Content-Type")
        headers.pop("Content-Length")
        headers["Authorization"] = f"JWT {self._access}"
        session.headers = headers
        return session

    async def get_shared_skud(self):
        session = await self.get_session()
        url = join(self.base_url, "api/v0/skud/shared/")
        async with self.lock:

            skud_list: Response = await self.hass.async_add_executor_job(
                partial(session.get, url)
            )
            skud_list.raise_for_status()
            return skud_list.json()

    async def open_skud(self, skud_id):
        session = await self.get_session()
        _LOGGER.debug("Start open skud %d", skud_id)
        url = join(self.base_url, f"api/v0/skud/shared/{skud_id}/open/")
        async with self.lock:
            skud_list: Response = await self.hass.async_add_executor_job(
                partial(session.get, url)
            )
            skud_list.raise_for_status()
            return skud_list.json()

    async def get_contract_info(self):
        session = await self.get_session()
        url = join(self.base_url, "api/v0/contract/")
        async with self.lock:
            contract_list: Response = await self.hass.async_add_executor_job(
                partial(session.get, url)
            )
            contract_list.raise_for_status()
            return contract_list.json()
