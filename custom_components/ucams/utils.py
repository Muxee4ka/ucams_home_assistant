import base64
import json
import functools
import asyncio
from transliterate import translit

import jwt

CONF_NAME = "name"
CONF_URL = "link"
CONF_DOM_URL = "dom_link"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CAMERA_IMAGE_REFRESH_INTERVAL = "camera_image_refresh_interval"
DOMAIN = "ucams"
TOKEN_REFRESH_BUFFER = 300
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
VIDEO = "video"
WS_VIDEO = "ws_video"
SCREEN = "screen"


def decode_token(token):
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except:
        return json.loads(base64.b64decode(token.split(".")[0]).decode())


async def async_transliterate(text) -> str:
    """Асинхронный вариант translit()"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(translit, text, "ru", reversed=True))
