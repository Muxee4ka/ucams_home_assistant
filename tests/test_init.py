"""Tests for helpers in custom_components.ucams.__init__."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

import custom_components.ucams as ucams_init
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


def _registry_entries(*domains):
    return [SimpleNamespace(domain=d) for d in domains]


async def test_setup_retries_when_known_cameras_come_back_empty(config_entry, monkeypatch):
    """An empty /api/v1/cctv after cameras existed is an outage, not "no cameras"."""
    ucams_api = MagicMock(get_cameras_info=AsyncMock(return_value={}), close=AsyncMock())
    dom_api = MagicMock(close=AsyncMock())
    monkeypatch.setattr(ucams_init, "DomApi", MagicMock(return_value=dom_api))
    monkeypatch.setattr(ucams_init, "UcamsApi", MagicMock(return_value=ucams_api))
    monkeypatch.setattr(ucams_init.er, "async_get", MagicMock())
    monkeypatch.setattr(
        ucams_init.er,
        "async_entries_for_config_entry",
        MagicMock(return_value=_registry_entries("camera", "button")),
    )

    with pytest.raises(ConfigEntryNotReady):
        await ucams_init.async_setup_entry(MagicMock(), config_entry)
    ucams_api.close.assert_awaited_once()
    dom_api.close.assert_awaited_once()


def test_had_contract_cameras_ignores_city_only_entries(monkeypatch):
    """City cameras have no archive buttons — an account with none of its own is legit."""
    monkeypatch.setattr(ucams_init.er, "async_get", MagicMock())
    entries = MagicMock(return_value=_registry_entries("camera", "image", "geo_location"))
    monkeypatch.setattr(ucams_init.er, "async_entries_for_config_entry", entries)
    entry = SimpleNamespace(entry_id="1")

    assert ucams_init._had_contract_cameras(MagicMock(), entry) is False
    entries.return_value = _registry_entries("camera", "button")
    assert ucams_init._had_contract_cameras(MagicMock(), entry) is True


async def test_failed_public_cameras_schedule_a_reload(config_entry, monkeypatch):
    """City cameras that failed to load are picked up by a later reload."""
    config_entry.options = {**config_entry.options, "public_cameras": True}
    config_entry.async_on_unload = MagicMock()
    call_later = MagicMock()
    monkeypatch.setattr(ucams_init, "async_call_later", call_later)
    hass = MagicMock()
    hass.config.latitude, hass.config.longitude = 54.7, 55.9
    cameras_api = MagicMock(get_public_cameras_info=AsyncMock(side_effect=OSError("reset")))

    assert await ucams_init._async_load_public_cameras(hass, config_entry, cameras_api) == {}

    _, delay, retry = call_later.call_args.args
    assert delay == ucams_init.PUBLIC_CAMERAS_RETRY_DELAY
    config_entry.async_on_unload.assert_called_once_with(call_later.return_value)
    retry(None)
    hass.config_entries.async_schedule_reload.assert_called_once_with(config_entry.entry_id)
