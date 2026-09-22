import logging
from time import time
from urllib.parse import urljoin

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .utils import (
    AUTH_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CONTRACT_ID,
    CONF_DOM_URL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    PHONE_APPLICATION_ID,
    PHONE_COUNTRY_ID,
    TOKEN_REFRESH_BUFFER,
    decode_token,
)

_LOGGER = logging.getLogger(__name__)


HEADERS = {
    "Connection": "Keep-Alive",
    "User-Agent": "okhttp/4.9.0",
}
BASE_URL = "https://dom.ufanet.ru/"


class PhoneAuthError(Exception):
    """Raised when a phone-call auth step fails (see phone_auth_* below)."""


async def _phone_post(session, base_url, path, payload=None, headers=None):
    url = urljoin(base_url, path)
    async with session.post(url, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        return await resp.json()


async def phone_auth_init(session, base_url: str, phone: str) -> dict:
    """Start a call-auth attempt. Returns {phone_to_call, request_id, timeout}.

    The user then calls `phone_to_call` from `phone`; Ufanet recognises the
    incoming number. See memory phone-auth-flow for the full contract.
    """
    data = await _phone_post(
        session,
        base_url,
        "api/v4/phone_auth/call/init/",
        {
            "phone": phone,
            "application_id": PHONE_APPLICATION_ID,
            "country_id": PHONE_COUNTRY_ID,
        },
    )
    payload = data.get("data") or {}
    if not payload.get("request_id") or not payload.get("phone_to_call"):
        raise PhoneAuthError(f"Unexpected init response: {data}")
    return payload


async def phone_auth_contact_list(session, base_url: str, request_id: str) -> list[dict]:
    """Return the contracts tied to the calling number, once the call landed.

    Empty until Ufanet has registered the incoming call, so the caller polls.
    """
    data = await _phone_post(
        session, base_url, "api/v4/phone_auth/call/contact_list/", {"request_id": request_id}
    )
    return ((data.get("data") or {}).get("contracts")) or []


async def phone_auth_apply(session, base_url: str, request_id: str, contract_id: int) -> dict:
    """Exchange a confirmed call for the dom JWT pair {access, refresh}."""
    data = await _phone_post(
        session,
        base_url,
        "api/v4/phone_auth/call/apply/",
        {"request_id": request_id, "contract_id": contract_id},
    )
    payload = data.get("data") or {}
    if not payload.get("access") or not payload.get("refresh"):
        raise PhoneAuthError(f"Unexpected apply response: {data}")
    return payload


class DomApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self.config_entry = config_entry
        opts = config_entry.options
        data = config_entry.data
        # dom_link lives in options for password entries; phone entries created
        # by the config flow put it in data too, so fall back gracefully.
        self.base_url = opts.get(CONF_DOM_URL) or data.get(CONF_DOM_URL) or BASE_URL
        # Phone accounts have no password: they seed the JWT pair captured
        # during the call-auth flow and can only ever renew via the refresh
        # token. Password accounts keep the original behaviour untouched.
        self.auth_method = data.get(CONF_AUTH_METHOD, opts.get(CONF_AUTH_METHOD))
        self.session = aiohttp.ClientSession(
            headers=HEADERS,
            trust_env=True,
            connector=aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
            ),
        )
        self.token: str | None = None
        self.token_expiration: int = 0
        # Refresh token issued alongside `access` by /auth_by_contract/ and by
        # the call-auth /apply/ step. Lives ~5 months and lets us renew access
        # without re-sending the password. The refresh response rotates *both*
        # tokens, so we update the stored refresh on each renewal.
        self.refresh_token: str | None = None
        self.refresh_token_expiration: int = 0

        if self.auth_method == AUTH_PHONE:
            self.username = data.get(CONF_CONTRACT_ID)
            self.password = None
            self._store_tokens(data[CONF_ACCESS_TOKEN], data.get(CONF_REFRESH_TOKEN))
        else:
            self.username = opts[CONF_USERNAME]
            self.password = opts[CONF_PASSWORD]

    def _store_tokens(self, access: str, refresh: str | None, persist: bool = False) -> None:
        self.token = access
        self.token_expiration = int(decode_token(access).get("exp", 0))
        self.session.headers["Authorization"] = f"JWT {access}"
        if refresh:
            self.refresh_token = refresh
            self.refresh_token_expiration = int(decode_token(refresh).get("exp", 0))
        if persist and self.auth_method == AUTH_PHONE:
            # Phone accounts can't re-login from scratch, so the rotated refresh
            # token must survive a restart — write it back to the config entry.
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_ACCESS_TOKEN: self.token,
                    CONF_REFRESH_TOKEN: self.refresh_token,
                },
            )

    async def _authenticate(self):
        if self.auth_method == AUTH_PHONE:
            # No password to fall back on; the refresh token is dead. The user
            # must re-run the call-auth flow via reauth.
            raise ConfigEntryAuthFailed(
                "Phone-auth session expired; re-add the integration to sign in by call again"
            )
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
        self._store_tokens(access, data.get("refresh"), persist=True)
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

    async def register_push_device(self, token: str, device_id: str, title: str) -> None:
        """Bind an FCM registration token to this account.

        Same call the Android app makes after `FirebaseMessaging.getToken()`.
        Re-posting an existing `device_id` just refreshes its token, so it is
        safe to run on every start. The device then shows up in the app's
        «active devices» list.
        """
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/fcm/")
        payload = {
            "token": token,
            "device_id": device_id,
            "title": title,
            "application": PHONE_APPLICATION_ID,
            "os": 0,
            "token_type": 0,
        }
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()

    async def unregister_push_device(self, device_id: str) -> None:
        """Drop an FCM registration.

        Not a harmless unsubscribe: Ufanet also revokes the refresh token of the
        session behind that device (the access token lives on until it
        expires). Password accounts just log in again; call-auth accounts end
        up in reauth.
        """
        session = await self.get_authenticated_session()
        url = urljoin(self.base_url, "api/v0/fcm/")
        async with session.delete(url, json={"device_id": device_id}) as resp:
            resp.raise_for_status()

    async def close(self):
        await self.session.close()
