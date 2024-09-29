import asyncio
import datetime
import logging
from functools import partial

from time import time
from urllib.parse import urljoin as join, urlunparse, urlencode

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, ufanet_api: "DomApi"
    ):
        self.hass = hass
        self._ufanet_api = ufanet_api
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.lock = asyncio.Lock()
        self._session = None
        self._expiration_date = None
        self.cameras = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]
        self.cams_server = None

    @property
    async def session(self):
        if (
            self._session is None
            or datetime.datetime.now().timestamp() >= self.expiration_date
        ):
            session = await self._ufanet_api.get_session()
            if not self.cams_server:
                contract = await self._ufanet_api.get_contract_info()
                self.cams_server = (
                    contract[0].get("isp_org", {}).get("cams_server", {}).get("url")
                )
            if not self.cams_server:
                raise
            url = join(self.cams_server, "api/v0/auth/?ttl=20800")

            self._session = requests.session()
            self._session.headers = HEADERS.copy()
            self._session.headers["Authorization"] = session.headers.get(
                "Authorization"
            )
            async with self.lock:
                r = await self.hass.async_add_executor_job(
                    partial(
                        self._session.post,
                        url,
                        allow_redirects=True,
                    )
                )
                _LOGGER.debug(r)
                _LOGGER.debug(r.content)

                token_info = r.json()

                self._expiration_date = decode_token(token_info["token"]).get("exp")
                self._session.headers["Authorization"] = f"Bearer {token_info['token']}"
        return self._session

    @property
    def expiration_date(self) -> int:
        return self._expiration_date

    async def get_cameras_info(self) -> dict:
        await self.session
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
        url = join(self.cams_server, "api/v0/cameras/my/")

        async with self.lock:
            r = await self.hass.async_add_executor_job(
                partial(
                    self._session.post,
                    url,
                    json=json_data,
                    allow_redirects=True,
                )
            )
            _LOGGER.debug(r)
            _LOGGER.debug(r.content)

            cameras_info = r.json()
            if cameras_info.get("count") > page_size:
                for page in range(2, cameras_info.get("count") // page_size + 1):
                    json_data["page"] = page
                    async with self.lock:
                        r = await self.hass.async_add_executor_job(
                            partial(self._session.post, url, json=json_data)
                        )
                        _LOGGER.debug(r)
                        _LOGGER.debug(r.content)
                        r.raise_for_status()

                        cameras_info["results"].extend(r.json().get("results"))
            scheme = "https"
            ws_scheme = "wss"
            for cam in cameras_info["results"]:
                cam_id = cam["number"]
                token_l = cam["token_l"]
                domain = cam["server"]["domain"]
                screenshot_domain = cam["server"]["screenshot_domain"]
                title = cam["title"]

                base_url = f"rtsp://{cam.get("server").get('domain')}"
                rtsp_link = f"{base_url}/{cam.get("number")}?token={cam.get('token_l')}&tracks=v1a1"
                ws_video = urlunparse(
                    (
                        ws_scheme,
                        domain,
                        join(cam_id, "mse_ld"),
                        "",
                        urlencode(
                            {
                                "tracks": "a1v1",
                                "realtime": "true",
                                "token": token_l,
                            }
                        ),
                        "",
                    )
                )

                path_screenshot = join("api/v0/screenshots/", f"{cam_id}~600.jpg")
                query_screenshot = urlencode({"token": token_l})
                url_screen = urlunparse(
                    (
                        scheme,
                        screenshot_domain,
                        path_screenshot,
                        "",
                        query_screenshot,
                        "",
                    )
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
        _LOGGER.debug(camera_info)
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
        result = await self.get_camera_url(camera_id, SCREEN)
        if result:
            async with self.lock:
                r = await self.hass.async_add_executor_job(
                    partial(
                        self._session.get,
                        result,
                        allow_redirects=True,
                    )
                )
            return r.content
