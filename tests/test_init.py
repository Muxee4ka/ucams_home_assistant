"""Tests for helpers in custom_components.ucams.__init__."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.ucams import (
    _async_register_static_assets,
    _normalize_archive_duration,
    _normalize_archive_start,
)


def test_normalize_archive_start_passes_int_through():
    """Legacy callers pass a unix timestamp directly — must round-trip."""
    assert _normalize_archive_start(1714915200) == 1714915200


def test_normalize_archive_start_converts_datetime_to_unix():
    """Datetime selector hands us a datetime object."""
    dt = datetime(2026, 5, 7, 14, 30, 0)
    expected = int(dt.timestamp())
    assert _normalize_archive_start(dt) == expected


def test_normalize_archive_duration_passes_int_through():
    assert _normalize_archive_duration(3600) == 3600


def test_normalize_archive_duration_converts_timedelta_to_seconds():
    """Duration selector → cv.time_period yields a timedelta."""
    assert _normalize_archive_duration(timedelta(hours=1, minutes=5)) == 3900


async def test_register_static_assets_runs_once():
    """Routes can't be unregistered — a second entry must not re-register them."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()

    await _async_register_static_assets(hass)
    await _async_register_static_assets(hass)

    registered = (
        hass.http.async_register_static_paths.await_count
        + hass.http.register_static_path.call_count
    )
    assert registered == 1
