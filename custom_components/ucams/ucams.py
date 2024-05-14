import asyncio
import datetime
import logging
from functools import partial
from os.path import join
from time import time
from urllib.parse import urlencode, urlunparse

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from transliterate import translit

from custom_components.ucams.utils import (
    CONF_URL,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_NAME,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    TOKEN_REFRESH_BUFFER,
    USER_AGENT,
    VIDEO,
    WS_VIDEO,
    SCREEN, decode_token,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


class UcamsApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self.username = config_entry.options[CONF_USERNAME]
        self.password = config_entry.options[CONF_PASSWORD]
        self.base_url = config_entry.options[CONF_URL]
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.lock = asyncio.Lock()
        self.session = requests.Session()
        self._expiration_date = None
        self.cameras = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]

    async def login(self):
        url = join(self.base_url, 'api/internal/login/')
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': USER_AGENT,
        }
        data = {
            'username': self.username,
            'password': self.password
        }
        async with self.lock:
            await self.hass.async_add_executor_job(
                partial(self.session.post, url, headers=headers, data=data))

    async def auth(self):
        if self._expiration_date is None:
            await self.login()
        else:
            await self.refresh_auth()

    @property
    def expiration_date(self) -> int:
        if self._expiration_date is None:
            cookies_received = self.session.cookies.get_dict()
            decoded_auth_token = decode_token(cookies_received["sessionid"]).get('exp')
            self._expiration_date = decoded_auth_token['exp']
        return self._expiration_date

    async def refresh_auth(self):
        if datetime.datetime.now().timestamp() >= self.expiration_date:
            await self.login()

    async def get_cameras_info(self) -> dict:
        await self.auth()
        url = join(self.base_url, 'api/v0/cameras/my/')
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
        }

        json_data = {
            'order_by': 'addr_asc',
            'fields': [
                'number',
                'address',
                'title',
                'longitude',
                'latitude',
                'is_embed',
                'analytics',
                'is_fav',
                'is_public',
                'inactivity_period',
                'server',
                'tariff',
                'token_l',
                'permission',
                'record_disable_period',
            ],
            'token_l_ttl': 86400,
            'page': 1,
            'page_size': 60,
        }

        async with self.lock:
            r = await self.hass.async_add_executor_job(
                partial(
                    self.session.post,
                    url,
                    headers=headers,
                    json=json_data,
                    allow_redirects=True,
                )
            )
            _LOGGER.debug(r)
            _LOGGER.debug(r.content)

            cameras_info = r.json()
            scheme = 'https'
            ws_scheme = "wss"
            for camera_info in cameras_info['results']:
                cam_id = camera_info["number"]
                token_l = camera_info["token_l"]
                domain = camera_info["server"]["domain"]
                screenshot_domain = camera_info["server"]["screenshot_domain"]
                title = camera_info["title"]

                path = join(cam_id, 'tracks-v1', 'mono.m3u8')
                query_video = urlencode({'token': token_l})
                url_video = urlunparse((scheme, domain, path, '', query_video, ''))
                ws_video = urlunparse(
                    (
                        ws_scheme,
                        domain,
                        join(cam_id, "mse_ld"),
                        '',
                        urlencode(
                            {
                                'tracks': 'a1v1',
                                'realtime': 'true',
                                'token': token_l,
                            }),
                        ""
                    )
                )

                path_screenshot = join("api", "v0", "screenshots", f"{cam_id}~600.jpg")
                query_screenshot = urlencode({'token': token_l})
                url_screen = urlunparse((scheme, screenshot_domain, path_screenshot, '', query_screenshot, ''))

                self.cameras[cam_id] = {
                    "id": cam_id,
                    "title": title,
                    "url_video": url_video,
                    "url_ws_video": ws_video,
                    "url_screen": url_screen,
                    "token_l": token_l
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
        _LOGGER.info(camera_info)
        if not camera_info:
            return None
        now = int(time())
        token_exp = decode_token(camera_info["token_l"]).get('exp')
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
                        self.session.get,
                        result,
                        allow_redirects=True,
                    )
                )
            return r.content
