import datetime
import logging
from functools import cached_property

from homeassistant.components.camera import Camera, CameraEntityFeature, async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .ucams import UcamsApi
from .utils import DOMAIN, TOKEN_REFRESH_BUFFER, build_object_id, parse_house_area

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()
    entities = [
        Ucams(hass, config_entry, cameras_api, camera_info) for camera_info in cameras_info.values()
    ]
    hass.data[config_entry.entry_id]["camera_entities"] = {e.entity_id: e for e in entities}
    async_add_entities(entities)


class Ucams(Camera):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: UcamsApi,
        camera_info: dict,
    ) -> None:
        super().__init__()

        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.camera_id = camera_info["id"]
        self.device_name = cameras_api.build_device_name(camera_info["title"])
        self._suggested_area = parse_house_area(camera_info.get("address"))
        self.entity_id = f"camera.{build_object_id(self.device_name, self.camera_id)}"

        self._attr_unique_id = f"camera-{self.camera_id}"
        self._attr_name = self.device_name
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._stream_refresh_cancel_fn = async_track_time_interval(
            self.hass,
            self._stream_refresh,
            datetime.timedelta(seconds=TOKEN_REFRESH_BUFFER),
        )

    async def _stream_refresh(self, now: datetime.datetime) -> None:
        _LOGGER.debug("Checking if stream url should be updated for camera %s", self.camera_id)
        url = await self.stream_source()
        if self.stream and self.stream.source != url:
            _LOGGER.debug("Updating camera %s stream source to %s", self.camera_id, url)
            self.stream.update_source(url)

    async def async_will_remove_from_hass(self) -> None:
        if self._stream_refresh_cancel_fn:
            self._stream_refresh_cancel_fn()

    async def stream_source(self) -> str | None:
        url = await self.cameras_api.get_camera_stream_url(self.camera_id)
        _LOGGER.debug("Camera %s stream source is %s", self.camera_id, url)
        return url

    @cached_property
    def use_stream_for_stills(self) -> bool:
        # No still-image endpoint upstream — keyframes are pulled from RTSP.
        return True

    @property
    def device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.device_name,
            "manufacturer": "Ufanet",
        }
        if self._suggested_area:
            info["suggested_area"] = self._suggested_area
        return info

    async def handle_snapshot_from_rtsp(self) -> bytes | None:
        """Grab a single frame using HA's stream component (same path as the
        UI screenshot button)."""
        try:
            image = await async_get_image(self.hass, self.entity_id, timeout=15)
        except Exception as err:
            _LOGGER.exception("Snapshot failed for camera %s: %r", self.camera_id, err)
            return None
        return image.content

    async def get_camera_archive(self, start_time, duration):
        archive_url = await self.cameras_api.get_camera_archive(
            self.camera_id, start_time, duration
        )
        if not archive_url:
            _LOGGER.error("ARCHIVE URL не получен для камеры %s", self.camera_id)
            return None
        return archive_url
