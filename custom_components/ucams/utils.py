import base64
import json

import jwt

CONF_NAME = "name"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CAMERA_IMAGE_REFRESH_INTERVAL = "camera_image_refresh_interval"
DOMAIN = "ucams"
TOKEN_REFRESH_BUFFER = 300
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'
VIDEO = "video"
SCREEN = "screen"


def decode_token(token):
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except:
        return json.loads(base64.b64decode(token.split(".")[0]).decode())

