"""Opt-in FCM listener that wakes the call-history coordinator on intercom calls.

Ufanet has no webhook/websocket for calls. The «Умный дом» app learns about
them from FCM data pushes with `data.reason == "sip"`, so we run a headless
FCM client (no Android / Play Services) registered as one more device of the
account via `POST /api/v0/fcm/`.

The push is only a wake-up signal: it triggers an immediate call-history
refresh, and the event/sensor entities fire from history as they always did.
`push.data.uuid` is not the history record's uuid (only `time` matches
`called_at`), history carries the camera the entities key on, and polling keeps
working as a fallback if a push gets lost.

The FCM state (Firebase credentials, our Ufanet `device_id`, recently seen
persistent ids) lives in a per-entry `Store`, so a restart reuses the same
registration instead of piling up «Home Assistant» rows in the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from firebase_messaging import FcmPushClient, FcmRegisterConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .ufanet import DomApi
from .utils import (
    DOMAIN,
    FCM_API_KEY,
    FCM_APP_ID,
    FCM_DEVICE_TITLE,
    FCM_PROJECT_ID,
    FCM_SENDER_ID,
    PHONE_APPLICATION_ID,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
# The library dedups redelivered pushes by persistent id; it only needs the
# recent ones.
MAX_PERSISTENT_IDS = 50
# GCM registration flakes with PHONE_REGISTRATION_ERROR every now and then and
# the MCS link can drop for good, so (re)starts back off up to half an hour.
RETRY_DELAYS = (10, 30, 60, 300, 900, 1800)
WATCHDOG_INTERVAL = 60
SAVE_DELAY = 10

OnCall = Callable[[datetime | None], Awaitable[None]]


class _DropRoutineDisconnects(logging.Filter):
    """Mute the library's traceback for a plain dropped MCS connection.

    On some Russian ISPs the TLS link to mtalk.google.com:5228 is cut exactly
    20s after login, heartbeats or not (the same client stays up from a German
    VPS). The library reconnects in ~0.5s and Google queues pushes meanwhile,
    so nothing is lost — but it logs `Unexpected exception during read` with a
    full traceback every time, thousands a day. Other errors still get through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (
            isinstance(exc, (EOFError, ConnectionResetError))
            and record.getMessage().startswith("Unexpected exception during read")
        )


logging.getLogger("firebase_messaging.fcmpushclient").addFilter(_DropRoutineDisconnects())


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.push.{entry_id}")


def _parse_push_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PushListener:
    """Keeps one FCM registration alive for a config entry."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, dom_api: DomApi, on_call: OnCall
    ) -> None:
        self.hass = hass
        self._dom_api = dom_api
        self._on_call = on_call
        self._store = _store(hass, entry_id)
        self._state: dict[str, Any] = {}
        self._client: FcmPushClient | None = None
        self._runner: asyncio.Task | None = None
        self._call_tasks: set[asyncio.Task] = set()

    async def async_start(self) -> None:
        """Load state and connect in the background — never blocks entry setup."""
        self._state = await self._store.async_load() or {}
        self._state.setdefault("device_id", f"{FCM_DEVICE_TITLE}_{uuid.uuid4()}")
        self._state.setdefault("persistent_ids", [])
        self._runner = self.hass.async_create_background_task(
            self._async_run(), f"{DOMAIN} push listener"
        )

    async def async_stop(self) -> None:
        """Disconnect but keep the Ufanet registration (reload/restart path)."""
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        for task in self._call_tasks:
            task.cancel()
        await self._async_stop_client()
        await self._store.async_save(self._state)

    async def _async_run(self) -> None:
        """Connect, then watch the link and reconnect with backoff when it dies.

        The library gives up for good after a few failed connects or an
        unexpected error, so without this a flaky night would silently turn
        push off until the next HA restart.
        """
        failures = 0
        while True:
            if await self._async_connect():
                failures = 0
                await self._async_watch()
            else:
                delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
                failures += 1
                _LOGGER.warning("Ucams push: connect failed, retrying in %ss", delay)
                await asyncio.sleep(delay)

    async def _async_watch(self) -> None:
        """Return once the client has been down for two checks in a row."""
        down = 0
        while down < 2:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            # RESETTING is a normal few-second blip, hence two strikes.
            down = 0 if self._client and self._client.is_started() else down + 1
        _LOGGER.warning("Ucams push: FCM connection lost, reconnecting")
        await self._async_stop_client()

    async def _async_connect(self) -> bool:
        await self._async_stop_client()
        config = FcmRegisterConfig(
            project_id=FCM_PROJECT_ID,
            app_id=FCM_APP_ID,
            api_key=FCM_API_KEY,
            messaging_sender_id=FCM_SENDER_ID,
            bundle_id=PHONE_APPLICATION_ID,
            persistend_ids=list(self._state["persistent_ids"]),
        )
        client = FcmPushClient(
            self._handle_push,
            config,
            credentials=self._state.get("credentials"),
            credentials_updated_callback=self._handle_credentials,
            received_persistent_ids=list(self._state["persistent_ids"]),
            http_client_session=async_get_clientsession(self.hass),
        )
        self._client = client
        try:
            token = await client.checkin_or_register()
            # Every start: the token may have rotated, and it bumps the row's
            # last_update in the app's device list.
            await self._dom_api.register_push_device(
                token, self._state["device_id"], FCM_DEVICE_TITLE
            )
            await client.start()
        except Exception as err:
            _LOGGER.warning("Ucams push: registration failed: %s", err)
            return False
        self._state["registered"] = True
        self._store.async_delay_save(lambda: self._state, SAVE_DELAY)
        _LOGGER.info("Ucams push: listening for intercom calls")
        return True

    async def _async_stop_client(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.stop()
        except Exception as err:
            _LOGGER.debug("Ucams push: error stopping FCM client: %s", err)
        self._client = None

    def _handle_credentials(self, credentials: dict[str, Any]) -> None:
        self._state["credentials"] = credentials
        self._store.async_delay_save(lambda: self._state, SAVE_DELAY)

    def _handle_push(self, notification: dict[str, Any], persistent_id: str, _ctx: Any) -> None:
        if persistent_id:
            ids = self._state["persistent_ids"]
            ids.append(persistent_id)
            del ids[:-MAX_PERSISTENT_IDS]
            self._store.async_delay_save(lambda: self._state, SAVE_DELAY)

        data = notification.get("data") if isinstance(notification, dict) else None
        if not isinstance(data, dict) or data.get("reason") != "sip":
            _LOGGER.debug("Ucams push: ignoring %s", data.get("reason") if data else None)
            return
        # The payload also carries live SIP credentials — never log it whole.
        _LOGGER.debug("Ucams push: intercom call at %s", data.get("time"))
        task = self.hass.async_create_task(self._on_call(_parse_push_time(data.get("time"))))
        self._call_tasks.add(task)
        task.add_done_callback(self._call_tasks.discard)


async def async_remove_registration(hass: HomeAssistant, entry_id: str, dom_api: DomApi) -> None:
    """Drop our FCM device from the account and forget its local state.

    Only for when the entry goes away or push is switched off on a password
    account: `DELETE /api/v0/fcm/` also revokes the refresh token of the
    session that registered it (see `DomApi.unregister_push_device`).
    """
    store = _store(hass, entry_id)
    state = await store.async_load()
    if not state:
        return
    if state.get("registered") and state.get("device_id"):
        try:
            await dom_api.unregister_push_device(state["device_id"])
        except Exception as err:
            _LOGGER.warning("Ucams push: failed to unregister FCM device: %s", err)
    await store.async_remove()
