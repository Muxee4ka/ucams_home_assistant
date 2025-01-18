import asyncio
import logging
from functools import partial
from pprint import pformat
from time import time
from urllib.parse import urljoin

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from transliterate import translit

from custom_components.ucams.utils import (
    CONF_NAME,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    TOKEN_REFRESH_BUFFER,
    VIDEO,
    WS_VIDEO,
    SCREEN,
    decode_token,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)

HEADERS = {
    "Accept-Language": "ru_RU",
    "User-Agent": "OnePlus NE2211 Android app: Smarthome, OS: 9",
    "Content-Type": "application/json",
}


class UcamsApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, ufanet_api: "DomApi"):
        self.hass = hass
        self._ufanet_api = ufanet_api
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.cameras = {}
        self.camera_image_refresh_interval = config_entry.options[CONF_CAMERA_IMAGE_REFRESH_INTERVAL]
        self.cams_server = None
        self.token = None
        self.token_expiration = 0
        self.session = ClientSession(headers=HEADERS)

    async def _authenticate(self):
        cams_servers = set()
        if not self.cams_server:

            contracts = await self._ufanet_api.get_contract_info()
            _LOGGER.debug(pformat(contracts))
            for contract in contracts:
                cams_servers.add(contract.get("isp_org", {}).get("cams_server", {}).get("url"))

        if not cams_servers and not self.cams_server:
            raise ConfigEntryNotReady("Cams server URL not available")
        if len(cams_servers) > 1:
            _LOGGER.warning("Multiple cams servers found: %s", cams_servers)
        self.cams_server = next(iter(cams_servers), self.cams_server)
        url = urljoin(self.cams_server, "api/v0/auth/?ttl=20800")
        self.session.headers["Authorization"] = self._ufanet_api.session.headers.get(
                "Authorization"
            )
        async with self.session.post(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            _LOGGER.debug(pformat(data))
            self.token = data["token"]
            self.token_expiration = decode_token(self.token).get("exp", 0)
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    async def get_authenticated_session(self):
        now = asyncio.get_running_loop().time()
        if (
                not self.token
                or now >= self.token_expiration - TOKEN_REFRESH_BUFFER
                or self._ufanet_api.token_expiration < now
        ):
            await self._authenticate()
        return self.session

    async def get_cameras_info(self) -> dict:
        session = await self.get_authenticated_session()
        page = 1
        page_size = 60

        json_data = {
            "order_by": "addr_asc",
            "fields": [
                "number",
                "address",
                "title",
                "longitude",
                "latitude",
                "is_embed",
                "analytics",
                "is_fav",
                "is_public",
                "inactivity_period",
                "server",
                "tariff",
                "token_l",
                "permission",
                "record_disable_period",
            ],
            "token_l_ttl": 86400,
            "page": page,
            "page_size": page_size,
        }
        url = urljoin(self.cams_server, "api/v0/cameras/my/")

        cameras_info = {"results": []}
        while True:
            async with session.post(url, json=json_data) as resp:
                status_code = resp.status
                if status_code == 401:
                    _LOGGER.error("Authentication failed. Trying to re-authenticate")
                    await self._authenticate()
                    return await self.get_cameras_info()
                resp.raise_for_status()
                response_data = await resp.json()
                cameras_info["results"].extend(response_data.get("results", []))
                if len(response_data.get("results", [])) < page_size:
                    break
                page += 1
                json_data["page"] = page

        for cam in cameras_info["results"]:
            cam_id = cam["number"]
            token_l = cam["token_l"]
            domain = cam["server"]["domain"]
            screenshot_domain = cam["server"]["screenshot_domain"]
            title = cam["title"]

            rtsp_link = f"rtsp://{domain}/{cam_id}?token={token_l}&tracks=v1a1"
            ws_video = urljoin(
                f"wss://{domain}", f"{cam_id}/mse_ld?tracks=a1v1&realtime=true&token={token_l}"
            )
            url_screen = urljoin(
                f"https://{screenshot_domain}", f"api/v0/screenshots/{cam_id}~600.jpg?token={token_l}"
            )

            self.cameras[cam_id] = {
                "id": cam_id,
                "title": title,
                "url_video": rtsp_link,
                "url_ws_video": ws_video,
                "url_screen": url_screen,
                "token_l": token_l,
            }

        return self.cameras

    def build_device_name(self, device_title) -> str:
        device_name = device_title.lower()
        device_name = f"{self.config_entry_name}.{device_name}"
        device_name = translit(device_name, "ru", reversed=True)
        return device_name.capitalize()

    async def get_camera_info(self, camera_id: str) -> dict | None:
        if camera_id not in self.cameras:
            await self.get_cameras_info()
        return self.cameras.get(camera_id)

    async def get_camera_url(self, camera_id: str, url_type: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)
        _LOGGER.debug(pformat(camera_info))
        if not camera_info:
            return None
        now = int(time())
        token_exp = decode_token(camera_info["token_l"]).get("exp")
        if token_exp and (int(token_exp) - now) < TOKEN_REFRESH_BUFFER:
            await self.get_cameras_info()
            camera_info = await self.get_camera_info(camera_id)
        return camera_info.get(f"url_{url_type}")

    async def get_camera_stream_ws_url(self, camera_id: str) -> str | None:
        result = await self.get_camera_url(camera_id, WS_VIDEO)
        return result

    async def get_camera_stream_url(self, camera_id: str):
        result = await self.get_camera_url(camera_id, VIDEO)
        return result

    async def get_camera_image(self, camera_id: str) -> str | None:
        session = await self.get_authenticated_session()
        result = await self.get_camera_url(camera_id, SCREEN)
        if result:
            async with session.get(result) as resp:
                resp.raise_for_status()
                content = await resp.read()
                return content
