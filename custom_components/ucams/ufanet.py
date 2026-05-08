import logging
from time import time
from urllib.parse import urljoin

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .utils import (
    CONF_DOM_URL,
    CONF_PASSWORD,
    CONF_USERNAME,
    TOKEN_REFRESH_BUFFER,
    decode_token,
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
        self.token: str | None = None
        self.token_expiration: int = 0
        # Refresh token issued alongside `access` by /auth_by_contract/.
        # Lives ~5 months and lets us renew access without re-sending the
        # password. The refresh response rotates *both* tokens, so we update
        # the stored refresh on each renewal.
        self.refresh_token: str | None = None
        self.refresh_token_expiration: int = 0

    def _store_tokens(self, access: str, refresh: str | None) -> None:
        self.token = access
        self.token_expiration = int(decode_token(access).get("exp", 0))
        self.session.headers["Authorization"] = f"JWT {access}"
        if refresh:
            self.refresh_token = refresh
            self.refresh_token_expiration = int(decode_token(refresh).get("exp", 0))

    async def _authenticate(self):
        url = urljoin(self.base_url, "api/v1/auth/auth_by_contract/")
        payload = {"contract": self.username, "password": self.password}
        try:
            async with self.session.post(url, json=payload, compress=False) as resp:
                if resp.status in (401, 403):
                    raise ConfigEntryAuthFailed(
                        f"Authentication rejected by Ufanet ({resp.status})"
                    )
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise ConfigEntryNotReady(f"Authentication request failed: {err}") from err

        token = data["token"]
        self._store_tokens(token["access"], token.get("refresh"))

    async def _refresh_access(self) -> bool:
        """Try to renew access via the refresh token. Returns True on success.

        POST /api/v1/auth/refresh/ accepts `{"token": "<refresh-jwt>"}` and
        returns a flat `{"access": ..., "refresh": ..., "exp": ...}` (note:
        not nested under a "token" key like the login response). The refresh
        token rotates on each call.
        """
        if not self.refresh_token:
            return False
        now = int(time())
        if self.refresh_token_expiration and now >= self.refresh_token_expiration:
            # Refresh JWT itself is dead; fall back to full login.
            return False
        url = urljoin(self.base_url, "api/v1/auth/refresh/")
        try:
            async with self.session.post(url, json={"token": self.refresh_token}) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Refresh rejected by Ufanet (%s); will full-login", resp.status)
                    return False
                data = await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.debug("Refresh request errored (%s); will full-login", err)
            return False

        access = data.get("access")
        if not access:
            return False
        self._store_tokens(access, data.get("refresh"))
        return True

    async def get_authenticated_session(self):
        # token_expiration is a unix timestamp from the JWT, so compare against
        # wall-clock time, not loop.time() (which is monotonic from process start).
        now = int(time())
        access_stale = not self.token or now >= self.token_expiration - TOKEN_REFRESH_BUFFER
        if access_stale and not await self._refresh_access():
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

    async def get_cctv_list(self) -> list[dict]:
        """Return the flat camera list from /api/v1/cctv.

        Each item carries number, title, address, latitude, longitude, type,
        inactivity_period, token_l (live), token_r (record), and
        servers.{domain, screenshot_domain, vendor_name}. Replaces the heavier
        cams_server /api/v0/cameras/my/ flow for the read path; cams_server is
        still required for archive (only it issues token_d).
        """
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v1/cctv")
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_call_history(self, page_size: int = 20):
        """Get recent intercom call history.

        Each item carries uuid, house_id, address, porch, flat, called_at,
        camera_number, skud_mac, timezone.
        """
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v1/skuds/call-history/")
        params = {"page": 1, "page_size": page_size}
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self):
        await self.session.close()
