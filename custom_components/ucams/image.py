import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .ucams import UcamsApi
from .utils import CONF_CAMERA_IMAGE_REFRESH_INTERVAL, DOMAIN, build_object_id


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()
    entities = [
        UcamsCameraImageEntity(hass, config_entry, cameras_api, camera_info)
        for camera_info in cameras_info.values()
    ]
    async_add_entities(entities)


class UcamsCameraImageEntity(ImageEntity):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: UcamsApi,
        camera_info: dict,
    ) -> None:
        super().__init__(hass)

        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.camera_id = camera_info["id"]
        self.device_name = self.cameras_api.build_device_name(camera_info["title"])
        self.entity_id = f"image.{build_object_id(self.device_name, self.camera_id)}"
        self._attr_unique_id = f"image-{self.camera_id}"
        self._attr_name = self.device_name
        self._attr_icon = "mdi:image-area"
        refresh_seconds = config_entry.options[CONF_CAMERA_IMAGE_REFRESH_INTERVAL]
        self._refresh_cancel_fn = async_track_time_interval(
            hass,
            self._async_mark_stale,
            datetime.timedelta(seconds=refresh_seconds),
        )

    async def async_image(self) -> bytes | None:
        res = await self.cameras_api.get_camera_image(self.camera_id)
        if res is not None:
            self._attr_image_last_updated = datetime.datetime.now()
        return res

    async def _async_mark_stale(self, now: datetime.datetime) -> None:
        # Bumping image_last_updated tells HA the cached image is stale; the
        # frontend will then call async_image() and re-fetch.
        self._attr_image_last_updated = now
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._refresh_cancel_fn:
            self._refresh_cancel_fn()

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.device_name,
            "manufacturer": "Ufanet",
        }
