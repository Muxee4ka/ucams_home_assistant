import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .ucams import UcamsApi
from .ufanet import DomApi
from .utils import DOMAIN, build_object_id, parse_house_area

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    dom_api = hass.data[config_entry.entry_id]["dom_api"]
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    skud_list = await dom_api.get_shared_skud()
    entities = []
    for skud_info in skud_list:
        camera_id = skud_info.get("cctv_number")
        _LOGGER.debug("SKUD: %s", skud_info)
        _LOGGER.debug("Camera ID: %s", camera_id)
        camera_info = await cameras_api.get_camera_info(camera_id) if camera_id else None
        entities.append(
            DomUfanetSwitchEntity(hass, config_entry, cameras_api, dom_api, skud_info, camera_info)
        )
    async_add_entities(entities)


class DomUfanetSwitchEntity(SwitchEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: UcamsApi,
        dom_api: DomApi,
        skud_info: dict,
        camera_info: dict | None,
    ) -> None:
        super().__init__()

        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.dom_api = dom_api
        self.skud_id = skud_info["id"]
        self.camera_id = skud_info.get("cctv_number")  # may be None
        if camera_info:
            self.device_name = self.cameras_api.build_device_name(camera_info["title"])
        else:
            self.device_name = self.cameras_api.build_device_name(
                skud_info["string_view"] + "_" + str(self.skud_id)
            )
        # Prefer the camera's address (richer); fall back to the skud's string_view.
        self._address = (camera_info or {}).get("address") or skud_info.get("string_view")
        self._suggested_area = parse_house_area(self._address)
        self.entity_id = f"switch.{build_object_id(self.device_name, self.skud_id)}"
        self._attr_unique_id = f"switch-{self.skud_id}"
        self._attr_name = self.device_name
        self._attr_icon = "mdi:door-open"
        self._attr_is_on = False
        self.time_out = skud_info["timeout"]
        self._auto_off_task: asyncio.Task | None = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open the intercom for `timeout` seconds (momentary)."""
        res = await self.dom_api.open_skud(self.skud_id)
        _LOGGER.debug(res)
        self.hass.bus.async_fire(
            f"{DOMAIN}_door_opened",
            {
                "entity_id": self.entity_id,
                "skud_id": self.skud_id,
                "camera_id": self.camera_id,
                "device_name": self.device_name,
                "address": self._address,
            },
        )
        self._attr_is_on = True
        self.async_write_ha_state()
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()
        self._auto_off_task = self.hass.async_create_task(self._auto_turn_off())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel the pending auto-off and flip back immediately."""
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()
        self._auto_off_task = None
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _auto_turn_off(self) -> None:
        try:
            await asyncio.sleep(self.time_out)
        except asyncio.CancelledError:
            return
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()

    @property
    def device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {
                (
                    DOMAIN,
                    f"{self.config_entry_id}_{self.camera_id if self.camera_id else self.skud_id}",
                )
            },
            "name": self.device_name,
            "manufacturer": "Ufanet",
        }
        if self._suggested_area:
            info["suggested_area"] = self._suggested_area
        return info
