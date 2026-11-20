"""Motion sensors fed by Ufanet's motion-detector events."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.ucams.binary_sensor as binary_sensor
import custom_components.ucams.coordinator as coordinator_module
from custom_components.ucams.binary_sensor import MOTION_GRACE, UcamsMotionSensor
from custom_components.ucams.coordinator import build_motion_coordinator

NOW = datetime(2026, 11, 20, 17, 10, tzinfo=UTC)
CAMERA = {"id": "1762776185KFV40", "title": "1, Камера 4"}


def _event(camera, start, length, event_id=1):
    return {
        "id": event_id,
        "date": start.isoformat().replace("+00:00", "Z"),
        "length": length,
        "camera_number": camera,
        "type": "motion_alarm",
    }


def _cameras_api(events_by_call):
    from custom_components.ucams.ucams import UcamsApi

    api = MagicMock()
    api.get_motion_events = AsyncMock(side_effect=events_by_call)
    api.build_display_name = lambda camera: camera["title"]
    api.build_device_name = lambda title: UcamsApi.build_device_name(
        SimpleNamespace(config_entry_name="Ucams"), title
    )
    return api


@pytest.fixture
def frozen_now(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(coordinator_module.dt_util, "utcnow", lambda: clock.now)
    monkeypatch.setattr(binary_sensor.dt_util, "utcnow", lambda: clock.now)
    # A real DataUpdateCoordinator needs a running HA (frame helper); these
    # tests only drive its update_method.
    monkeypatch.setattr(
        coordinator_module,
        "DataUpdateCoordinator",
        lambda hass, logger, **kwargs: SimpleNamespace(**kwargs),
    )
    return clock


@pytest.mark.asyncio
async def test_coordinator_keeps_the_event_that_ends_last(frozen_now):
    long_one = _event("A", NOW - timedelta(minutes=5), 345, 1)
    later_short = _event("A", NOW - timedelta(minutes=3), 60, 2)
    api = _cameras_api([[later_short, long_one], []])
    coordinator = build_motion_coordinator(MagicMock(), api, ["A"])

    data = await coordinator.update_method()
    assert data["A"]["id"] == 1
    assert data["A"]["length"] == 345

    start, end = api.get_motion_events.call_args.args[1:]
    assert start == NOW - coordinator_module.MOTION_WINDOW
    assert end > NOW

    # The event left the window: the camera keeps it.
    assert (await coordinator.update_method())["A"]["id"] == 1


@pytest.mark.asyncio
async def test_coordinator_failure_is_update_failed(frozen_now):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    api = _cameras_api(RuntimeError("boom"))
    coordinator = build_motion_coordinator(MagicMock(), api, ["A"])
    with pytest.raises(UpdateFailed):
        await coordinator.update_method()


def test_motion_sensor_follows_the_event_with_grace(frozen_now):
    start = NOW - timedelta(seconds=50)
    event = {"id": 1, "start": start, "length": 60}
    coordinator = MagicMock(data={CAMERA["id"]: event})
    sensor = UcamsMotionSensor(coordinator, "E1", _cameras_api([]), CAMERA)

    assert sensor.is_on
    assert sensor.extra_state_attributes == {"started_at": start.isoformat(), "duration": 60}

    frozen_now.now = start + timedelta(seconds=60) + MOTION_GRACE - timedelta(seconds=1)
    assert sensor.is_on
    frozen_now.now = start + timedelta(seconds=60) + MOTION_GRACE
    assert not sensor.is_on


def test_motion_sensor_without_events_is_off(frozen_now):
    sensor = UcamsMotionSensor(MagicMock(data={}), "E1", _cameras_api([]), CAMERA)

    assert not sensor.is_on
    assert sensor.extra_state_attributes is None


def test_motion_sensor_ids_and_device(frozen_now):
    sensor = UcamsMotionSensor(MagicMock(data={}), "E1", _cameras_api([]), CAMERA)

    assert sensor.has_entity_name
    assert sensor.translation_key == "motion"
    assert sensor.unique_id == "E1_motion-1762776185KFV40"
    assert sensor.entity_id == "binary_sensor.ucams_1_kamera_4_motion_1762776185kfv40"
    assert sensor.device_info["name"] == "1, Камера 4"
    assert sensor.device_info["identifiers"] == {("ucams", "E1_1762776185KFV40")}
