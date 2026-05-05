import asyncio
import datetime
import logging
import re

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .ucams import UcamsApi
from .utils import DOMAIN, TOKEN_REFRESH_BUFFER

FFMPEG_SNAPSHOT_TIMEOUT = 15

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()
    entities = [
        Ucams(hass, config_entry, cameras_api, camera_info) for camera_info in cameras_info.values()
    ]
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
        device_slug = re.sub(r"[^a-z0-9]+", "_", self.device_name.lower()).strip("_")
        camera_slug = re.sub(r"[^a-z0-9]+", "_", str(self.camera_id).lower()).strip("_")
        object_id = f"{device_slug}_{camera_slug}" if camera_slug else device_slug
        self.entity_id = f"camera.{object_id}"

        self._attr_unique_id = f"camera-{self.camera_id}"
        self._attr_name = self.device_name
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._stream_refresh_cancel_fn = async_track_time_interval(
            self.hass,
            self._stream_refresh,
            datetime.timedelta(seconds=TOKEN_REFRESH_BUFFER),
        )
        self._entity_picture = None

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

    # async_camera_image is intentionally not overridden: with
    # CameraEntityFeature.STREAM advertised, the base Camera class fetches a
    # keyframe from stream_source via the public path. The previous override
    # called the private _async_get_stream_image, which can break on HA
    # upgrades, and was equivalent to the default anyway.

    async def async_update(self):
        """Update camera entity."""
        self._entity_picture = self.cameras_api.get_camera_image(self.camera_id)

    @property
    def entity_picture(self) -> str | None:
        """Return the camera image URL."""
        return self._entity_picture

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.device_name,
            "manufacturer": "Ufanet",
        }

    async def handle_snapshot_from_rtsp(self) -> bytes | None:
        """Grab a single frame from the camera's RTSP stream via ffmpeg."""
        rtsp_url = await self.cameras_api.get_camera_stream_url(self.camera_id)
        if not rtsp_url:
            _LOGGER.error("RTSP URL не найден для камеры %s", self.camera_id)
            return None

        # Argument list — never a shell string — so the URL (which contains
        # tokens and query params) is passed as a single argv entry safely.
        args = [
            "ffmpeg",
            "-i",
            rtsp_url,
            "-vf",
            "select=eq(n\\,0)",
            "-vframes",
            "1",
            "-q:v",
            "2",
            "-f",
            "image2",
            "-",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            _LOGGER.error("Не удалось запустить ffmpeg для камеры %s: %s", self.camera_id, err)
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=FFMPEG_SNAPSHOT_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.error("FFmpeg для камеры %s превысил тайм-аут", self.camera_id)
            process.kill()
            await process.wait()
            return None

        if process.returncode != 0:
            _LOGGER.error(
                "Ошибка FFmpeg для камеры %s: %s",
                self.camera_id,
                stderr.decode(errors="replace"),
            )
            return None

        _LOGGER.info("Снимок успешно получен для камеры %s", self.camera_id)
        return stdout

    async def get_camera_archive(self, start_time, duration):
        archive_url = await self.cameras_api.get_camera_archive(
            self.camera_id, start_time, duration
        )
        if not archive_url:
            _LOGGER.error("ARCHIVE URL не получен для камеры %s", self.camera_id)
            return None
        return archive_url
