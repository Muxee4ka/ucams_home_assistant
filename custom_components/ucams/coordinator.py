"""Call-history coordinator, shared by the sensor and event platforms.

Built in `async_setup_entry` before the platforms are forwarded: HA sets
platforms up concurrently, so neither of them can own it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ufanet import DomApi

_LOGGER = logging.getLogger(__name__)

CALL_HISTORY_PAGE_SIZE = 20
CALL_HISTORY_SCAN_INTERVAL = timedelta(seconds=30)
# After a push, history had the call on the first request in every live test
# (~0.6s later); the retries only cover a slower publish.
PUSH_REFRESH_DELAYS = (0, 1, 2, 5)
# push.data.time and called_at matched to the second, allow one of skew.
PUSH_TIME_SLACK = timedelta(seconds=1)


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
