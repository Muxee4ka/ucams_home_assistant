"""Doorbell event per intercom, fired for each new call-history record.

Works in plain polling mode (within the 30s scan interval); with push
notifications on, the FCM listener refreshes history right after the call so
the event lands within about a second.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .coordinator import parse_called_at
from .utils import DOMAIN, build_object_id

_LOGGER = logging.getLogger(__name__)

EVENT_RING = "ring"
# History that shows up this late is catch-up after an outage, not a ring worth
# announcing — nobody should get a «someone's at the door» for last hour.
STALE_CALL_AGE = timedelta(minutes=5)
_CALL_FIELDS = ("uuid", "called_at", "address", "porch", "flat", "camera_number")


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[config_entry.entry_id]
    dom_api = data["dom_api"]
    cameras_api = data["cameras_api"]
    coordinator = data["call_history_coordinator"]

    try:
        skud_list = await dom_api.get_shared_skud()
    except Exception as err:
        _LOGGER.warning("Failed to fetch skud list for call events: %s", err)
        return

    entities = []
    for skud_info in skud_list:
        camera_id = skud_info.get("cctv_number")
        if not camera_id:
            # History rows are keyed by camera_number; same rule as LastCallSensor.
            continue
        camera_info = await cameras_api.get_camera_info(camera_id)
        device_name = (
            cameras_api.build_device_name(camera_info["title"])
            if camera_info
            else cameras_api.build_device_name(
                f"{skud_info.get('string_view', 'skud')}_{skud_info['id']}"
            )
        )
        entities.append(
            IntercomCallEvent(coordinator, config_entry.entry_id, camera_id, device_name)
        )
    async_add_entities(entities)


class IntercomCallEvent(CoordinatorEntity, EventEntity):
    """Fires `ring` once per call-history uuid of this intercom's camera."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_icon = "mdi:doorbell-video"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry_id: str,
        camera_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_event_types = [EVENT_RING]
        self._config_entry_id = config_entry_id
        self._camera_id = str(camera_id)
        self._device_name = device_name
        self._attr_name = f"Звонок {device_name}"
        self._attr_unique_id = f"ucams_intercom_call_{camera_id}"
        self.entity_id = f"event.{build_object_id(device_name, f'intercom_call_{camera_id}')}"
        # Whatever history holds at startup has already happened — don't replay it.
        self._seen = {c["uuid"] for c in self._matching_calls() if c.get("uuid")}

    def _matching_calls(self) -> list[dict]:
        items = self.coordinator.data or []
        return [item for item in items if str(item.get("camera_number")) == self._camera_id]

    @callback
    def _handle_coordinator_update(self) -> None:
        calls = [c for c in self._matching_calls() if c.get("uuid")]
        new = [c for c in calls if c["uuid"] not in self._seen]
        self._seen.update(c["uuid"] for c in calls)
        now = dt_util.utcnow()
        # History is newest-first; fire oldest-first so the last event wins.
        for call in reversed(new):
            called_at = parse_called_at(call.get("called_at"))
            if called_at is None or called_at.tzinfo is None:
                continue
            if now - called_at > STALE_CALL_AGE:
                _LOGGER.debug("Skipping stale call %s at %s", call["uuid"], called_at)
                continue
            self._trigger_event(EVENT_RING, {k: call.get(k) for k in _CALL_FIELDS})
            self.async_write_ha_state()
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self._config_entry_id}_{self._camera_id}")},
            "name": self._device_name,
            "manufacturer": "Ufanet",
        }
