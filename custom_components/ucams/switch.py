import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .ucams import UcamsApi
from .ufanet import DomApi
from .utils import DOMAIN, build_object_id, parse_house_area, scoped_unique_id

_LOGGER = logging.getLogger(__name__)

# How long the "open" switch stays on before flipping back. The barrier closes
# itself; the pulse just gives history/logbook something to show.
BARRIER_AUTO_OFF_DELAY = 1.0


async def async_setup_entry(hass, config_entry, async_add_entities):
    dom_api = hass.data[config_entry.entry_id]["dom_api"]
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    entities = []
    for skud_info in await dom_api.get_shared_skud():
        camera_id = skud_info.get("cctv_number")
        _LOGGER.debug("SKUD: %s", skud_info)
        _LOGGER.debug("Camera ID: %s", camera_id)
        camera_info = await cameras_api.get_camera_info(camera_id) if camera_id else None
        entities.append(
            DomUfanetSwitchEntity(hass, config_entry, cameras_api, dom_api, skud_info, camera_info)
        )
    for barrier_info in hass.data[config_entry.entry_id].get("barriers_info") or []:
        entities.append(UcamsBarrierSwitchEntity(hass, config_entry, cameras_api, barrier_info))
    async_add_entities(entities)


class DomUfanetSwitchEntity(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "door"

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
        # device_name stays the transliterated slug source: it is also what the
        # `ucams_door_opened` event has always carried.
        self.device_name, self._device_title = cameras_api.build_skud_device_names(
            skud_info, camera_info
        )
        # Prefer the camera's address (richer); fall back to the skud's string_view.
        self._address = (camera_info or {}).get("address") or skud_info.get("string_view")
        self._suggested_area = parse_house_area(self._address)
        self.entity_id = f"switch.{build_object_id(self.device_name, self.skud_id)}"
        self._attr_unique_id = scoped_unique_id(self.config_entry_id, f"switch-{self.skud_id}")
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
            "name": self._device_title,
            "manufacturer": "Ufanet",
        }
        if self._suggested_area:
            info["suggested_area"] = self._suggested_area
        return info


class UcamsBarrierSwitchEntity(SwitchEntity):
    """Momentary "open barrier" switch — one per device from /api/v0/devices/.

    The barrier closes itself after opening, so unlike the intercom switch
    there is no upstream-defined hold time: the entity pulses on for
    `BARRIER_AUTO_OFF_DELAY` and drops back.
    """

    _attr_has_entity_name = True
    # The barrier is its own device; the switch is that device.
    _attr_name = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: UcamsApi,
        barrier_info: dict,
    ) -> None:
        super().__init__()
        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.barrier_id = barrier_info["id"]
        self.module_ids = barrier_info.get("module_ids") or []
        self._multi_module_warned = False

        title = barrier_info.get("title") or f"Шлагбаум {self.barrier_id}"
        # The device carries the raw Russian title (same convention as cameras);
        # the transliterated form only feeds the entity_id slug.
        self._title = title
        self._attr_unique_id = scoped_unique_id(self.config_entry_id, f"barrier-{self.barrier_id}")
        self.entity_id = (
            f"switch.{build_object_id(cameras_api.build_device_name(title), self.barrier_id)}"
        )
        self._attr_icon = "mdi:boom-gate-arrow-up-outline"
        self._attr_is_on = False
        # An inactive barrier is kept in the registry but disabled: it can't be
        # opened, so an enabled switch would only produce failed commands.
        self._attr_entity_registry_enabled_default = bool(barrier_info.get("is_active", True))
        self._auto_off_task: asyncio.Task | None = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the open command (momentary)."""
        # A device-wide command (`module_id: null`) is accepted by the API but
        # doesn't open every barrier — a single-module one stayed shut until
        # its module was named (#19). So always target a module when there is
        # one; with several, which one a press should drive is a UI decision
        # we haven't made yet, so drive the first and say so.
        module_id = self.module_ids[0] if self.module_ids else None
        if len(self.module_ids) > 1 and not self._multi_module_warned:
            _LOGGER.warning(
                "Barrier %s has %s modules (%s); opening module %s. "
                "Per-module switches may follow once the API is confirmed live.",
                self.barrier_id,
                len(self.module_ids),
                self.module_ids,
                module_id,
            )
            self._multi_module_warned = True
        await self.cameras_api.open_barrier(self.barrier_id, module_id)
        self.hass.bus.async_fire(
            f"{DOMAIN}_barrier_opened",
            {
                "entity_id": self.entity_id,
                "barrier_id": self.barrier_id,
                "module_id": module_id,
                "device_name": self._title,
            },
        )
        self._attr_is_on = True
        self.async_write_ha_state()
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()
        self._auto_off_task = self.hass.async_create_task(self._auto_turn_off())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Flip back immediately."""
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()
        self._auto_off_task = None
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _auto_turn_off(self) -> None:
        try:
            await asyncio.sleep(BARRIER_AUTO_OFF_DELAY)
        except asyncio.CancelledError:
            return
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._auto_off_task and not self._auto_off_task.done():
            self._auto_off_task.cancel()

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_barrier_{self.barrier_id}")},
            "name": self._title,
            "manufacturer": "Ufanet",
            "model": "Шлагбаум",
        }
