import asyncio
import base64
import json
import logging
import re
import datetime
import shlex
import subprocess

import websockets
from homeassistant.components.camera import Camera, CameraEntityFeature, _async_get_stream_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import now

from . import UcamsApi
from .utils import TOKEN_REFRESH_BUFFER, DOMAIN, TIMEOUT

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

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
            camera_info: dict,
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
        )

        self._attr_unique_id = f"camera-{self.entity_id}"
        self._attr_name = self.device_name
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._stream_refresh_cancel_fn = async_track_time_interval(
            self.hass,
            self._stream_refresh,
            datetime.timedelta(seconds=TOKEN_REFRESH_BUFFER),
        )

    async def _stream_refresh(self, now: datetime.datetime) -> None:
        _LOGGER.info(
            "Checking if stream url should be updated for camera %s", self.camera_id
        )
        url = await self.stream_source()
        if self.stream and self.stream.source != url:
            _LOGGER.info("Updating camera %s stream source to %s", self.camera_id, url)
            self.stream.update_source(url)

    async def async_will_remove_from_hass(self) -> None:
        if self._stream_refresh_cancel_fn:
            self._stream_refresh_cancel_fn()

    async def stream_source(self) -> str | None:
        url = await self.cameras_api.get_camera_stream_url(self.camera_id)
        _LOGGER.info("Camera %s stream source is %s", self.camera_id, url)
        return url

    async def async_camera_image(
            self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.cameras_api.get_camera_image(self.camera_id)

    @property
    def device_info(self) -> DeviceInfo:
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry_id}_{self.camera_id}")},
            "name": self.device_name,
        }

    async def handle_snapshot(self):
        async with asyncio.timeout(TIMEOUT):
            image = await _async_get_stream_image(self, wait_for_next_keyframe=True)
            if image is None:
                return

            return image

    async def handle_snapshot_from_ws(self):
        """Получение скриншота с потока камеры через websocket"""
        uri = await self.cameras_api.get_camera_stream_ws_url(self.camera_id)
        async with asyncio.timeout(TIMEOUT):
            async with websockets.connect(uri) as websocket:
                # Получение и обработка инициализационного сегмента
                await websocket.send("resume")
                init_segment_msg = await websocket.recv()
                init_segment = json.loads(init_segment_msg)
                init_payload = init_segment['tracks'][0]['payload']
                init_data = base64.b64decode(init_payload)

                # Настройка FFmpeg для извлечения одного кадра
                command = r'ffmpeg -i - -vf "select=eq(n\,0)" -vframes 1 -f image2 -'
                ffmpeg_cmd = subprocess.Popen(
                    shlex.split(command),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False
                )

                try:
                    # Передача инициализационного сегмента
                    ffmpeg_cmd.stdin.write(init_data)

                    while True:
                        message = await websocket.recv()
                        if isinstance(message, bytes):
                            ffmpeg_cmd.stdin.write(message)
                            break
                        else:
                            _LOGGER.debug("Non-bytes message received: %s", message)
                    output_stream, error_stream = ffmpeg_cmd.communicate()
                    output_bytes = output_stream
                    return output_bytes

                except websockets.ConnectionClosed:
                    _LOGGER.error("WebSocket connection closed")
                finally:
                    ffmpeg_cmd.terminate()
                    ffmpeg_cmd.wait()

