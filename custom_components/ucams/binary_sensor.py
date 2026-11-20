"""Motion sensor per camera, fed by Ufanet's own motion detector.

The app's «События» on a camera are `motion_alarm` analytics events on
cams_server. There is no push for them, so they are polled (see
`coordinator.build_motion_coordinator`); an event is listed the moment motion
starts, so the sensor turns on within one scan interval.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .coordinator import motion_end
from .ucams import UcamsApi
from .utils import DOMAIN, build_object_id, scoped_unique_id

# An ongoing event's `length` is stretched only after its current end has
# passed (60s → ~115s showed up 64s after the start), so the sensor holds on a
# bit past the end it knows about rather than blinking off between polls.
MOTION_GRACE = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[config_entry.entry_id]
    coordinator = data.get("motion_coordinator")
    if coordinator is None:
        return
    cameras_info = data["cameras_info"]
    async_add_entities(
        UcamsMotionSensor(coordinator, config_entry.entry_id, data["cameras_api"], camera_info)
        for camera_id, camera_info in cameras_info.items()
        if camera_id in data["motion_cameras"]
    )


class UcamsMotionSensor(CoordinatorEntity, BinarySensorEntity):
    """On while the camera's latest motion event (plus a grace period) lasts."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_has_entity_name = True
    _attr_translation_key = "motion"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        cameras_api: UcamsApi,
        camera_info: dict,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry_id = config_entry_id
        self.camera_id = str(camera_info["id"])
        self._device_name = cameras_api.build_display_name(camera_info)
        slug_name = cameras_api.build_device_name(camera_info["title"])
        self._attr_unique_id = scoped_unique_id(config_entry_id, f"motion-{self.camera_id}")
        self.entity_id = f"binary_sensor.{build_object_id(slug_name, f'motion_{self.camera_id}')}"

    @property
    def _event(self) -> dict | None:
        return (self.coordinator.data or {}).get(self.camera_id)

    @property
    def is_on(self) -> bool:
        event = self._event
        if event is None:
            return False
        return dt_util.utcnow() < motion_end(event) + MOTION_GRACE

    @property
    def extra_state_attributes(self) -> dict | None:
        event = self._event
        if event is None:
            return None
        return {"started_at": event["start"].isoformat(), "duration": event["length"]}

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self._config_entry_id}_{self.camera_id}")},
            "name": self._device_name,
            "manufacturer": "Ufanet",
        }
