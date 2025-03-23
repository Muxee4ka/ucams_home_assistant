import datetime
import logging
import re
import shlex
import subprocess

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature, _async_get_stream_image,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import now

from . import UcamsApi
from .utils import TOKEN_REFRESH_BUFFER, DOMAIN

_LOGGER = logging.getLogger(__name__)



async def async_setup_entry(hass, config_entry, async_add_entities):
    cameras_api = hass.data[config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()
    entities = [
        Ucams(hass, config_entry, cameras_api, camera_info)
        for camera_info in cameras_info.values()
    ]
    async_add_entities(entities)


class Ucams(Camera):
    def __init__(
            self,
            hass: HomeAssistant,
            config_entry: ConfigEntry,
            cameras_api: UcamsApi,
            camera_info: dict
    ) -> None:
        super().__init__()

        self.hass = hass
        self.config_entry_id = config_entry.entry_id
        self.cameras_api = cameras_api
        self.camera_id = camera_info["id"]
        self.device_name = cameras_api.build_device_name(camera_info["title"])
        self.entity_id = (
                DOMAIN
                + "."
                + re.sub("[^a-zA-z0-9]+", "_", self.device_name).rstrip("_").lower()
                + "."
                + str(self.camera_id)
        )

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
        _LOGGER.debug(
            "Checking if stream url should be updated for camera %s", self.camera_id
        )
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

    async def async_camera_image(
            self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await _async_get_stream_image(self, wait_for_next_keyframe=True)

    async def async_update(self):
        """ Update camera entity. """
        self._entity_picture = self.cameras_api.get_camera_image(self.camera_id)

    @property
    def entity_picture(self) -> str | None:
        """ Return the camera image URL. """
        return self._entity_picture

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.device_name,
            "manufacturer": "Ufanet",
        }

    async def handle_snapshot_from_rtsp(self) -> bytes | None:
        """
        Get snapshot from RTSP stream.
        """
        rtsp_url = await self.cameras_api.get_camera_stream_url(self.camera_id)  # Получение RTSP URL потока
        if not rtsp_url:
            _LOGGER.error("RTSP URL не найден для камеры %s", self.camera_id)
            return None

        command = (
            f"ffmpeg -i {shlex.quote(rtsp_url)} -vf 'select=eq(n\\,0)' "
            f"-vframes 1 -q:v 2 -f image2 -"
        )

        _LOGGER.debug("Выполняется команда FFmpeg для RTSP: %s", command)

        # Запуск FFmpeg процесса
        ffmpeg_cmd = subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            output_stream, error_stream = ffmpeg_cmd.communicate(timeout=15)  # Тайм-аут 15 секунд

            if ffmpeg_cmd.returncode != 0:
                _LOGGER.error(
                    "Ошибка FFmpeg для камеры %s: %s",
                    self.camera_id,
                    error_stream.decode()
                )
                return None

            _LOGGER.info("Снимок успешно получен для камеры %s", self.camera_id)
            return output_stream

        except subprocess.TimeoutExpired:
            _LOGGER.error("FFmpeg для камеры %s превысил тайм-аут", self.camera_id)
            ffmpeg_cmd.terminate()
            return None

        except Exception as e:
            _LOGGER.error("Ошибка при выполнении FFmpeg для камеры %s: %s", self.camera_id, e)
            return None

        finally:
            ffmpeg_cmd.terminate()
            ffmpeg_cmd.wait()

    async def get_camera_archive(self, start_time, duration):
        archive_url = await self.cameras_api.get_camera_archive(self.camera_id, start_time, duration)
        if not archive_url:
            _LOGGER.error("ARCHIVE URL не получен для камеры %s", self.camera_id)
            return None
        return archive_url

