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
    hass.data = {}
    hass.config.latitude, hass.config.longitude = 54.7, 55.9
    cameras_api = MagicMock(get_public_cameras_info=AsyncMock(side_effect=OSError("reset")))

    assert await ucams_init._async_load_public_cameras(hass, config_entry, cameras_api) == {}

    _, delay, retry = call_later.call_args.args
    assert delay == ucams_init.PUBLIC_CAMERAS_RETRY_DELAY
    config_entry.async_on_unload.assert_called_once_with(call_later.return_value)
    retry(None)
    hass.config_entries.async_schedule_reload.assert_called_once_with(config_entry.entry_id)


async def test_public_cameras_retry_backs_off(config_entry, monkeypatch):
    """A failure that doesn't go away must not reload the entry every 10 min (#18)."""
    config_entry.options = {**config_entry.options, "public_cameras": True}
    config_entry.async_on_unload = MagicMock()
    call_later = MagicMock()
    monkeypatch.setattr(ucams_init, "async_call_later", call_later)
    hass = MagicMock()
    hass.data = {}
    hass.config.latitude, hass.config.longitude = 54.7, 55.9
    failing = AsyncMock(side_effect=OSError("403"))
    cameras_api = MagicMock(get_public_cameras_info=failing)

    delays = []
    for _ in range(8):
        await ucams_init._async_load_public_cameras(hass, config_entry, cameras_api)
        delays.append(call_later.call_args.args[1])

    base = ucams_init.PUBLIC_CAMERAS_RETRY_DELAY
    assert delays[:4] == [base, 2 * base, 4 * base, 8 * base]
    assert delays[-1] == ucams_init.PUBLIC_CAMERAS_RETRY_MAX_DELAY

    # One success starts the ladder over.
    failing.side_effect = None
    failing.return_value = {"cam": {}}
    assert await ucams_init._async_load_public_cameras(hass, config_entry, cameras_api) == {
        "cam": {}
    }
    failing.side_effect = OSError("reset")
    await ucams_init._async_load_public_cameras(hass, config_entry, cameras_api)
    assert call_later.call_args.args[1] == base


def test_assign_areas_keeps_the_users_choice(monkeypatch):
    """Runs on every setup — must not move a device the user already put somewhere (#17)."""
    entry = SimpleNamespace(entry_id="1")
    placed = SimpleNamespace(id="d1", identifiers={("ucams", "1_cam1")}, area_id="kitchen")
    unplaced = SimpleNamespace(id="d2", identifiers={("ucams", "1_cam2")}, area_id=None)
    area_reg = MagicMock()
    area_reg.async_get_area_by_name.return_value = None
    area_reg.async_get_or_create.return_value = SimpleNamespace(id="lenina_1")
    dev_reg = MagicMock()
    monkeypatch.setattr(ucams_init.ar, "async_get", MagicMock(return_value=area_reg))
    monkeypatch.setattr(ucams_init.dr, "async_get", MagicMock(return_value=dev_reg))
    monkeypatch.setattr(
        ucams_init.dr,
        "async_entries_for_config_entry",
        MagicMock(return_value=[placed, unplaced]),
    )
    cameras = {
        "cam1": {"address": "г. Уфа, Ленина, 1"},
        "cam2": {"address": "г. Уфа, Ленина, 1"},
    }

    ucams_init._assign_areas_by_address(MagicMock(), entry, cameras)

    dev_reg.async_update_device.assert_called_once_with("d2", area_id="lenina_1")
    area_reg.async_get_or_create.assert_called_once()


def test_assign_areas_creates_no_area_when_every_device_is_placed(monkeypatch):
    """A deleted address area must not come back if nothing would go into it."""
    entry = SimpleNamespace(entry_id="1")
    placed = SimpleNamespace(id="d1", identifiers={("ucams", "1_cam1")}, area_id="kitchen")
    area_reg = MagicMock()
    area_reg.async_get_area_by_name.return_value = None
    monkeypatch.setattr(ucams_init.ar, "async_get", MagicMock(return_value=area_reg))
    monkeypatch.setattr(ucams_init.dr, "async_get", MagicMock())
    monkeypatch.setattr(
        ucams_init.dr, "async_entries_for_config_entry", MagicMock(return_value=[placed])
    )

    ucams_init._assign_areas_by_address(
        MagicMock(), entry, {"cam1": {"address": "г. Уфа, Ленина, 1"}}
    )

    area_reg.async_get_or_create.assert_not_called()


def test_assign_areas_puts_a_barrier_next_to_its_camera(monkeypatch):
    """A barrier has no address — it follows the first of its cameras, wherever
    the user placed that camera. Barriers the user placed stay put."""
    entry = SimpleNamespace(entry_id="1")
    camera = SimpleNamespace(id="d1", identifiers={("ucams", "1_cam1")}, area_id="yard")
    barrier = SimpleNamespace(id="b1", identifiers={("ucams", "1_barrier_42")}, area_id=None)
    placed_barrier = SimpleNamespace(id="b2", identifiers={("ucams", "1_barrier_43")}, area_id="x")
    lonely_barrier = SimpleNamespace(id="b3", identifiers={("ucams", "1_barrier_44")}, area_id=None)
    dev_reg = MagicMock()
    monkeypatch.setattr(ucams_init.ar, "async_get", MagicMock())
    monkeypatch.setattr(ucams_init.dr, "async_get", MagicMock(return_value=dev_reg))
    monkeypatch.setattr(
        ucams_init.dr,
        "async_entries_for_config_entry",
        MagicMock(return_value=[camera, barrier, placed_barrier, lonely_barrier]),
    )
    barriers = [
        {"id": 42, "camera_numbers": ["unknown", "cam1"]},
        {"id": 43, "camera_numbers": ["cam1"]},
        {"id": 44, "camera_numbers": []},
    ]

    ucams_init._assign_areas_by_address(
        MagicMock(), entry, {"cam1": {"address": "г. Уфа, Ленина, 1"}}, barriers
    )

    dev_reg.async_update_device.assert_called_once_with("b1", area_id="yard")


def _reg_entity(entry_id, unique_id, created_at=None, domain="camera", device_id=None):
    return SimpleNamespace(
        platform="ucams",
        domain=domain,
        config_entry_id=entry_id,
        unique_id=unique_id,
        created_at=created_at,
        entity_id=f"{domain}.{unique_id.lower()}",
        device_id=device_id,
    )


def _two_entries(monkeypatch, entities):
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [
        SimpleNamespace(entry_id="A", disabled_by=None),
        SimpleNamespace(entry_id="B", disabled_by=None),
    ]
    registry = SimpleNamespace(entities={e.entity_id: e for e in entities})
    monkeypatch.setattr(ucams_init.er, "async_get", MagicMock(return_value=registry))
    return hass


def test_camera_of_unique_id_reads_scoped_and_legacy_ids():
    assert ucams_init._camera_of_unique_id("A_camera-CAM1", "A") == ("camera-", "CAM1")
    assert ucams_init._camera_of_unique_id("camera-CAM1", "A") == ("camera-", "CAM1")
    assert ucams_init._camera_of_unique_id("A_archive_button_CAM1_300", "A") == (
        "archive_button_",
        "CAM1",
    )
    assert ucams_init._camera_of_unique_id("A_switch-135542", "A") is None


def test_shared_camera_stays_with_the_entry_that_registered_it_first(monkeypatch):
    """An upgrade must keep the entities users already have: B registered
    CAM1 long ago, A's copy is new — A skips it. CAM2 is A's alone."""
    old, new = datetime(1970, 1, 1), datetime(2026, 11, 15)
    hass = _two_entries(
        monkeypatch,
        [
            _reg_entity("B", "B_camera-CAM1", old),
            _reg_entity("A", "A_camera-CAM1", new),
            _reg_entity("A", "A_camera-CAM2", new),
        ],
    )

    entry_a = SimpleNamespace(entry_id="A")
    entry_b = SimpleNamespace(entry_id="B")
    assert ucams_init._cameras_owned_elsewhere(hass, entry_a, {"CAM1", "CAM2"}) == {"CAM1"}
    assert ucams_init._cameras_owned_elsewhere(hass, entry_b, {"CAM1"}) == set()


def test_first_setup_of_a_shared_camera_keeps_it(monkeypatch):
    """Nobody has it registered yet (fresh install): both keep it; the next
    start settles on one owner by entry order."""
    hass = _two_entries(monkeypatch, [])
    assert ucams_init._cameras_owned_elsewhere(hass, SimpleNamespace(entry_id="B"), {"X"}) == set()

    same = datetime(2026, 11, 15)
    hass = _two_entries(
        monkeypatch, [_reg_entity("A", "A_camera-X", same), _reg_entity("B", "B_camera-X", same)]
    )
    assert ucams_init._cameras_owned_elsewhere(hass, SimpleNamespace(entry_id="B"), {"X"}) == {"X"}
    assert ucams_init._cameras_owned_elsewhere(hass, SimpleNamespace(entry_id="A"), {"X"}) == set()


def test_remove_camera_entities_drops_only_per_camera_entities(monkeypatch):
    entities = [
        _reg_entity("A", "A_camera-CAM1", device_id="dev1"),
        _reg_entity("A", "A_archive_button_CAM1_300", domain="button", device_id="dev1"),
        _reg_entity("A", "A_switch-135542", domain="switch", device_id="dev2"),
        _reg_entity("A", "A_camera-CAM2", device_id="dev3"),
    ]
    ent_reg = MagicMock()
    dev_reg = MagicMock()
    monkeypatch.setattr(ucams_init.er, "async_get", MagicMock(return_value=ent_reg))
    monkeypatch.setattr(ucams_init.dr, "async_get", MagicMock(return_value=dev_reg))
    monkeypatch.setattr(
        ucams_init.er, "async_entries_for_config_entry", MagicMock(return_value=entities)
    )
    monkeypatch.setattr(ucams_init.er, "async_entries_for_device", MagicMock(return_value=[]))

    ucams_init._remove_camera_entities(MagicMock(), SimpleNamespace(entry_id="A"), {"CAM1"})

    removed = [c.args[0] for c in ent_reg.async_remove.call_args_list]
    assert removed == ["camera.a_camera-cam1", "button.a_archive_button_cam1_300"]
    dev_reg.async_remove_device.assert_called_once_with("dev1")
