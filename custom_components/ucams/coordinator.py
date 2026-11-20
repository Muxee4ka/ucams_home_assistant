"""Coordinators built in `async_setup_entry` before the platforms are forwarded.

Call history is shared by the sensor and event platforms — HA sets platforms
up concurrently, so neither of them can own it. Motion events are built there
too, because whether there is anything to poll is only known after asking
cams_server which cameras have the detector.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .ucams import UcamsApi
from .ufanet import DomApi

_LOGGER = logging.getLogger(__name__)

CALL_HISTORY_PAGE_SIZE = 20
CALL_HISTORY_SCAN_INTERVAL = timedelta(seconds=30)
# After a push, history had the call on the first request in every live test
# (~0.6s later); the retries only cover a slower publish.
PUSH_REFRESH_DELAYS = (0, 1, 2, 5)
# push.data.time and called_at matched to the second, allow one of skew.
PUSH_TIME_SLACK = timedelta(seconds=1)

MOTION_SCAN_INTERVAL = timedelta(seconds=15)
# The API filters on the event's start, so the window has to outlast the
# longest event: Ufanet closes one after ~6 min (354s was the longest across
# 40 cameras over a week) and opens the next.
MOTION_WINDOW = timedelta(minutes=15)
# Ask a little past "now" so clock skew with the server can't hide an event.
MOTION_WINDOW_AHEAD = timedelta(minutes=1)


def parse_called_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # fromisoformat handles "2026-05-07T12:34:56+05:00" and Z-suffixed values on 3.11+
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_call_history_coordinator(hass: HomeAssistant, dom_api: DomApi) -> DataUpdateCoordinator:
    async def _update() -> list[dict]:
        data = await dom_api.get_call_history(page_size=CALL_HISTORY_PAGE_SIZE)
        if isinstance(data, dict):
            return data.get("results") or []
        if isinstance(data, list):
            return data
        return []

    return DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="ucams_call_history",
        update_method=_update,
        update_interval=CALL_HISTORY_SCAN_INTERVAL,
    )


def _has_call_since(calls: list[dict] | None, called_at: datetime) -> bool:
    threshold = called_at - PUSH_TIME_SLACK
    for item in calls or []:
        ts = parse_called_at(item.get("called_at"))
        if ts is not None and ts.tzinfo is not None and ts >= threshold:
            return True
    return False


async def async_refresh_for_call(
    coordinator: DataUpdateCoordinator, called_at: datetime | None
) -> None:
    """Refresh history until it shows the pushed call (or give up quietly).

    `async_refresh`, not `async_request_refresh`: the latter's debouncer would
    hold back the retries for its 10s cooldown.
    """
    for delay in PUSH_REFRESH_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        await coordinator.async_refresh()
        if called_at is None or called_at.tzinfo is None:
            return
        if _has_call_since(coordinator.data, called_at):
            return
    _LOGGER.debug("Call pushed at %s still missing from history", called_at)


def build_motion_coordinator(
    hass: HomeAssistant, cameras_api: UcamsApi, camera_ids: list[str]
) -> DataUpdateCoordinator:
    """Poll motion events; data is `{camera_id: {"id", "start", "length"}}`.

    Per camera it keeps the event that ends last. A camera keeps its last event
    after it has left the window, so `started_at` stays meaningful between
    motions.
    """
    latest: dict[str, dict] = {}

    async def _update() -> dict[str, dict]:
        now = dt_util.utcnow()
        try:
            events = await cameras_api.get_motion_events(
                camera_ids, now - MOTION_WINDOW, now + MOTION_WINDOW_AHEAD
            )
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Motion events request failed: {err}") from err
        for event in events:
            start = parse_called_at(event.get("date"))
            if start is None or start.tzinfo is None:
                continue
            camera_id = str(event.get("camera_number"))
            current = {
                "id": event.get("id"),
                "start": start,
                "length": int(event.get("length") or 0),
            }
            known = latest.get(camera_id)
            if known is None or motion_end(current) >= motion_end(known):
                latest[camera_id] = current
        return dict(latest)

    return DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="ucams_motion",
        update_method=_update,
        update_interval=MOTION_SCAN_INTERVAL,
    )


def motion_end(event: dict) -> datetime:
    return event["start"] + timedelta(seconds=event["length"])
