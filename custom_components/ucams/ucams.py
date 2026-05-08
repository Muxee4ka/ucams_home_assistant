import logging
from time import time
from urllib.parse import urljoin

import aiohttp
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .ufanet import DomApi
from .utils import (
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    CONF_NAME,
    SCREEN,
    TOKEN_REFRESH_BUFFER,
    VIDEO,
    WS_VIDEO,
    decode_token,
    transliterate_ru,
)

_LOGGER = logging.getLogger(__name__)


HEADERS = {
    "Accept-Language": "ru_RU",
    "User-Agent": "OnePlus NE2211 Android app: Smarthome, OS: 9",
    "Content-Type": "application/json",
}


class UcamsApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, ufanet_api: DomApi):
        self.hass = hass
        self._ufanet_api = ufanet_api
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.cameras = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]
        # cams_server is only needed for archive — discovered + authenticated lazily
        # when get_camera_archive is first called. Live streams + screenshots
        # come from dom.ufanet.ru/api/v1/cctv with the dom JWT alone.
        self.cams_server: str | None = None
        self.token: str | None = None
        self.token_expiration: int = 0
        self._cams_session: ClientSession | None = None

    async def _ensure_cams_session(self) -> ClientSession:
        """Lazily create + authenticate a session against cams_server.

        Only the archive flow needs this — see project_v1_cctv_hybrid_plan
        in the assistant's memory. cams_server URL is discovered from the
        contract response on first use.
        """
        if self._cams_session is None:
            self._cams_session = ClientSession(
                headers=HEADERS,
                connector=aiohttp.TCPConnector(
                    resolver=aiohttp.ThreadedResolver(),
                ),
            )
        now = int(time())
        if (
            self.token
            and now < self.token_expiration - TOKEN_REFRESH_BUFFER
            and self._ufanet_api.token_expiration > now
        ):
            return self._cams_session
        await self._authenticate_cams()
        return self._cams_session

    async def _authenticate_cams(self) -> None:
        if not self.cams_server:
            cams_servers = set()
            contracts = await self._ufanet_api.get_contract_info()
            for contract in contracts:
                cams_servers.add(contract.get("isp_org", {}).get("cams_server", {}).get("url"))
            cams_servers.discard(None)
            if not cams_servers:
                raise ConfigEntryNotReady("Cams server URL not available")
            if len(cams_servers) > 1:
                _LOGGER.warning("Multiple cams servers found: %s", cams_servers)
            self.cams_server = next(iter(cams_servers))

        assert self._cams_session is not None
        url = urljoin(self.cams_server, "api/v0/auth/?ttl=20800")
        # Bootstrap with dom JWT, then swap for cams_server bearer
        self._cams_session.headers["Authorization"] = self._ufanet_api.session.headers.get(
            "Authorization"
        )
        async with self._cams_session.post(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            self.token = data["token"]
            self.token_expiration = decode_token(self.token).get("exp", 0)
            self._cams_session.headers["Authorization"] = f"Bearer {self.token}"

    async def get_cameras_info(self) -> dict:
        """Fetch the camera list from dom /api/v1/cctv (one round-trip, no pagination)."""
        cctv = await self._ufanet_api.get_cctv_list()
        self.cameras = {}
        for cam in cctv:
            cam_id = cam["number"]
            token_l = cam["token_l"]
            servers = cam.get("servers") or {}
            domain = servers.get("domain")
            screenshot_domain = servers.get("screenshot_domain")
            title = cam.get("title")

            if not domain or not screenshot_domain:
                _LOGGER.warning("Camera %s missing domain info, skipping", cam_id)
                continue

            rtsp_link = f"rtsp://{domain}/{cam_id}?token={token_l}&tracks=v1a1"
            ws_video = urljoin(
                f"wss://{domain}", f"{cam_id}/mse_ld?tracks=a1v1&realtime=true&token={token_l}"
            )
            url_screen = urljoin(
                f"https://{screenshot_domain}",
                f"api/v0/screenshots/{cam_id}~600.jpg?token={token_l}",
            )

            self.cameras[cam_id] = {
                "id": cam_id,
                "title": title,
                "domain": domain,
                "url_video": rtsp_link,
                "url_ws_video": ws_video,
                "url_screen": url_screen,
                "token_l": token_l,
                "latitude": cam.get("latitude"),
                "longitude": cam.get("longitude"),
                "address": cam.get("address"),
            }

        return self.cameras

    def build_device_name(self, device_title) -> str:
        device_name = device_title.lower()
        device_name = f"{self.config_entry_name}.{device_name}"
        device_name = transliterate_ru(device_name)
        return device_name.capitalize()

    async def get_camera_info(self, camera_id: str) -> dict | None:
        if camera_id not in self.cameras:
            await self.get_cameras_info()
        return self.cameras.get(camera_id)

    async def get_camera_url(self, camera_id: str, url_type: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)
        if not camera_info:
            _LOGGER.error("Camera %s not found.", camera_id)
            return None

        now = int(time())
        token_exp = self._decode_token_exp(camera_info.get("token_l"))
        if token_exp and (token_exp - now) < TOKEN_REFRESH_BUFFER:
            _LOGGER.warning(
                "Camera token %s is about to expire (%s sec), refreshing cameras list.",
                camera_id,
                token_exp - now,
            )
            await self.get_cameras_info()
            camera_info = await self.get_camera_info(camera_id)

        token_exp = self._decode_token_exp(camera_info.get("token_l"))
        if not token_exp or (token_exp - now) < TOKEN_REFRESH_BUFFER:
            _LOGGER.error("Failed to update token for camera %s.", camera_id)
            return None

        url_key = f"url_{url_type}"
        url = camera_info.get(url_key)
        if not url:
            _LOGGER.error("URL (%s) not found for camera %s.", url_type, camera_id)
        else:
            _LOGGER.debug("URL (%s) for camera %s: %s", url_type, camera_id, url)
        return url

    def _decode_token_exp(self, token: str) -> int | None:
        try:
            decoded = decode_token(token)
            return int(decoded.get("exp", 0))
        except Exception as e:
            _LOGGER.error("Token decoding error: %s", e)
            return None

    async def get_camera_stream_ws_url(self, camera_id: str) -> str | None:
        return await self.get_camera_url(camera_id, WS_VIDEO)

    async def get_camera_stream_url(self, camera_id: str):
        return await self.get_camera_url(camera_id, VIDEO)

    async def get_camera_image(self, camera_id: str) -> bytes | None:
        """Pull the cached screenshot URL via the dom session.

        token_l from /api/v1/cctv works for the screenshot endpoint without
        any cams_server auth (verified empirically).
        """
        url = await self.get_camera_url(camera_id, SCREEN)
        if not url:
            return None
        session = await self._ufanet_api.get_authenticated_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def close(self) -> None:
        if self._cams_session is not None:
            await self._cams_session.close()
            self._cams_session = None

    async def get_camera_archive(self, camera_id: str, start_time: int, delta_time: int):
        """Get archive download URL.

        This is the only path that still needs cams_server: token_d (with
        embedded ds/dd archive window) is only issued by /api/v0/cameras/this/.
        token_r from /api/v1/cctv returns 403 against the archive endpoint.
        """
        session = await self._ensure_cams_session()
        camera_info = await self.get_camera_info(camera_id)
        if not camera_info:
            _LOGGER.error("Camera %s not found for archive request", camera_id)
            return None
        domain = camera_info.get("domain")

        params = {"lang": "ru"}
        json_data = {
            "fields": ["token_d"],
            "token_d_ttl": 3600,
            "token_d_duration": delta_time,
            "token_d_start": start_time,
            "numbers": [camera_id],
        }
        archive_url_endpoint = f"{self.cams_server}/api/v0/cameras/this/"

        for attempt in range(2):
            async with session.post(
                archive_url_endpoint, params=params, json=json_data
            ) as response:
                if response.status == 401 and attempt == 0:
                    _LOGGER.error("Cams auth failed, re-authenticating")
                    await self._authenticate_cams()
                    continue
                response.raise_for_status()
                response_data = await response.json()
                break

        result = response_data.get("results", [])
        if not result:
            return None
        for item in result:
            if item["number"] == camera_id:
                file_extension = ".mp4" if delta_time <= 3600 else ".ts"
                archive_url = (
                    f"https://{domain}/{item['number']}/"
                    f"archive-{start_time}-{delta_time}{file_extension}"
                    f"?token={item['token_d']}"
                )
                _LOGGER.debug(archive_url)
                return archive_url
