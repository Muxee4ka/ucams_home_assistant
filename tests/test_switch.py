"""Tests for barrier switch entities in custom_components.ucams.switch."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ucams import switch as switch_platform

BARRIER = {
    "id": 42,
    "title": "Шлагбаум во дворе",
    "is_active": True,
    "camera_numbers": [],
    "module_ids": [7],
    "emergency_vehicle_access": False,
}


class FakeHass:
    """Just what the barrier entity touches: the event bus and task creation."""

    def __init__(self):
        self.bus = SimpleNamespace(async_fire=MagicMock())
        self.data = {}

    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def _cameras_api():
    api = MagicMock()
    api.build_device_name = MagicMock(return_value="Test config.shlagbaum vo dvore")
    api.open_barrier = AsyncMock(return_value={"ok": True})
    return api


def _setup_hass(barriers):
    hass = FakeHass()
    hass.data["1"] = {
        "dom_api": MagicMock(get_shared_skud=AsyncMock(return_value=[])),
        "cameras_api": _cameras_api(),
        "barriers_info": barriers,
    }
    return hass


async def _run_setup(hass, config_entry):
    added = []
    await switch_platform.async_setup_entry(hass, config_entry, added.extend)
    return added


def _fresh_entity(hass, barrier):
    """A entity wired to a patched hass so state writes don't hit the registry."""
    entity = switch_platform.UcamsBarrierSwitchEntity(
        hass, SimpleNamespace(entry_id="1"), _cameras_api(), barrier
    )
    entity.async_write_ha_state = MagicMock()
    return entity


@pytest.mark.asyncio
async def test_setup_creates_one_switch_per_barrier(config_entry):
    hass = _setup_hass([BARRIER, {**BARRIER, "id": 43, "title": "Второй"}])

    entities = await _run_setup(hass, config_entry)

    assert [e.barrier_id for e in entities] == [42, 43]
    assert all(e.unique_id == f"1_barrier-{e.barrier_id}" for e in entities)
    # The switch is the barrier device itself: it takes the device name.
    assert all(e.has_entity_name and e.name is None for e in entities)
    assert all(e.is_on is False for e in entities)
    # Devices are separate from the intercom ones and carry the Russian title.
    assert entities[0].device_info["identifiers"] == {("ucams", "1_barrier_42")}
    assert entities[0].device_info["name"] == "Шлагбаум во дворе"


@pytest.mark.asyncio
async def test_setup_without_barriers_creates_nothing(config_entry):
    """Accounts without barriers must see no difference from before."""
    hass = _setup_hass([])

    assert await _run_setup(hass, config_entry) == []


@pytest.mark.asyncio
async def test_inactive_barrier_is_disabled_by_default(config_entry):
    hass = _setup_hass([{**BARRIER, "is_active": False}])

    (entity,) = await _run_setup(hass, config_entry)

    assert entity._attr_entity_registry_enabled_default is False


@pytest.mark.asyncio
async def test_active_barrier_is_enabled_by_default(config_entry):
    hass = _setup_hass([BARRIER])

    (entity,) = await _run_setup(hass, config_entry)

    assert entity._attr_entity_registry_enabled_default is True


@pytest.mark.asyncio
async def test_turn_on_single_module_targets_it(monkeypatch):
    """One module → still named: `module_id: null` left a barrier shut (#19)."""
    monkeypatch.setattr(switch_platform, "BARRIER_AUTO_OFF_DELAY", 0.01)
    hass = FakeHass()
    entity = _fresh_entity(hass, BARRIER)

    await entity.async_turn_on()

    entity.cameras_api.open_barrier.assert_awaited_once_with(42, 7)
    assert hass.bus.async_fire.call_count == 1
    event_type, event_data = hass.bus.async_fire.call_args.args
    assert event_type == "ucams_barrier_opened"
    assert event_data["barrier_id"] == 42
    assert event_data["module_id"] == 7
    assert entity.is_on is True

    await asyncio.sleep(0.05)
    assert entity.is_on is False  # momentary: flipped back without a timer


@pytest.mark.asyncio
async def test_turn_on_without_modules_sends_device_wide_command(monkeypatch):
    """No modules listed → nothing to name: fall back to `module_id: null`."""
    monkeypatch.setattr(switch_platform, "BARRIER_AUTO_OFF_DELAY", 0.01)
    hass = FakeHass()
    entity = _fresh_entity(hass, {**BARRIER, "module_ids": []})

    await entity.async_turn_on()

    entity.cameras_api.open_barrier.assert_awaited_once_with(42, None)
    await entity._auto_off_task


@pytest.mark.asyncio
async def test_turn_on_multi_module_targets_the_first(monkeypatch):
    """Several modules → the first one is driven, same as the app's MODULE mode."""
    monkeypatch.setattr(switch_platform, "BARRIER_AUTO_OFF_DELAY", 0.01)
    hass = FakeHass()
    entity = _fresh_entity(hass, {**BARRIER, "module_ids": [7, 8]})

    await entity.async_turn_on()

    entity.cameras_api.open_barrier.assert_awaited_once_with(42, 7)
    _, event_data = hass.bus.async_fire.call_args.args
    assert event_data["module_id"] == 7
    # Let the pulse finish: a pending task fails the test under the HA plugin.
    await entity._auto_off_task


@pytest.mark.asyncio
async def test_turn_off_cancels_the_pulse(monkeypatch):
    monkeypatch.setattr(switch_platform, "BARRIER_AUTO_OFF_DELAY", 5)
    hass = FakeHass()
    entity = _fresh_entity(hass, BARRIER)

    await entity.async_turn_on()
    auto_off = entity._auto_off_task
    await entity.async_turn_off()

    assert entity.is_on is False
    # The 5-second auto-off must never fire after an explicit turn-off.
    await asyncio.sleep(0)  # let the cancellation settle
    assert auto_off.cancelled()
