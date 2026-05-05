import base64
import binascii
import json

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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
VIDEO = "video"
WS_VIDEO = "ws_video"
SCREEN = "screen"


def decode_token(token: str) -> dict:
    """Decode a JWT payload without verifying the signature.

    Falls back to manually base64-decoding the first segment when PyJWT can't
    parse the token (some Ufanet endpoints hand back non-standard tokens).
    """
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        pass

    try:
        payload = token.split(".")[0]
        # base64 decode tolerant of missing padding
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError, IndexError):
        return {}
