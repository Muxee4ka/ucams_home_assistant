import asyncio
import logging
import re
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import UcamsApi, DomApi, DOMAIN

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

async def async_setup_entry(hass, config_entry, async_add_entities):
    dom_api = hass.data[config_entry.entry_id]["dom_api"]
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    skud_list = await dom_api.get_shared_skud()
    entities = []
    for skud_info in skud_list:
        camera_id = skud_info.get("cctv_number")
        camera_info = (
            await cameras_api.get_camera_info(camera_id) if camera_id else None
        )
        entities.append(
            DomUfanetSwitchEntity(
                hass, config_entry, cameras_api, dom_api, skud_info, camera_info
            )
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
        self.entity_id = (
            DOMAIN
            + "."
            + re.sub("[^a-zA-z0-9]+", "_", self.device_name).rstrip("_").lower()
        )
        self._attr_unique_id = f"switch-{self.entity_id}"
        self._attr_name = self.device_name
        self._attr_is_on = False
        self.time_out = skud_info["timeout"]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        res = await self.dom_api.open_skud(self.skud_id)
        _LOGGER.debug(res)
        self._attr_is_on = True
        self.auto_turn_off_task = asyncio.create_task(self.auto_turn_off())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        self._attr_is_on = False

    async def auto_turn_off(self) -> None:
        await asyncio.sleep(self.time_out)
        self._attr_is_on = False
        await self.hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": self.entity_id},
            blocking=False,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {
                (
                    DOMAIN,
                    f"{self.config_entry_id}_{self.camera_id if self.camera_id else self.skud_id}",
                )
            },
            "name": self.device_name,
        }