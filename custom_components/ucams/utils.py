import base64
import binascii
import json
import re

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


_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Mirrors transliterate>=1.10's "ru" reverse pack. Kept verbatim so existing
# entity_ids generated under that library are preserved across the swap.
_RU_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "'", "ы": "y", "ь": "'", "э": "e", "ю": "ju", "я": "ja",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "Zh", "З": "Z", "И": "I", "Й": "J", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Sch",
    "Ъ": "'", "Ы": "Y", "Ь": "'", "Э": "E", "Ю": "Ju", "Я": "Ja",
}


def transliterate_ru(value: str) -> str:
    return "".join(_RU_TRANSLIT.get(ch, ch) for ch in value)


_AREA_CITY_PREFIX_RE = re.compile(r"^\s*г\.\s*[^,]+,\s*", re.IGNORECASE)
_AREA_PORCH_SUFFIX_RE = re.compile(r"\s*,\s*п\.\s*\d+\s*$", re.IGNORECASE)


def parse_house_area(address: str | None) -> str | None:
    """Reduce an Ufanet address to a 'street, house' area name.

    "г. Нижний Новгород, Медицинская, 15к3, п.1" → "Медицинская, 15к3".
    Returns None for empty/unparseable input so the caller can skip area assignment.
    """
    if not address:
        return None
    cleaned = _AREA_CITY_PREFIX_RE.sub("", address)
    cleaned = _AREA_PORCH_SUFFIX_RE.sub("", cleaned)
    cleaned = cleaned.strip(" ,")
    return cleaned or None


def build_object_id(device_name: str, suffix: str | int | None) -> str:
    """Build a slugified object_id from a device name + optional suffix."""
    device_slug = _SLUG_RE.sub("_", device_name.lower()).strip("_")
    if suffix is None or suffix == "":
        return device_slug
    suffix_slug = _SLUG_RE.sub("_", str(suffix).lower()).strip("_")
    return f"{device_slug}_{suffix_slug}" if suffix_slug else device_slug


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
