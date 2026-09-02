import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .ucams import UcamsApi
from .ufanet import DomApi
from .utils import (
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    CONF_DOM_URL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PUBLIC_CAMERAS,
    CONF_PUBLIC_CAMERAS_QUERY,
    CONF_PUBLIC_CAMERAS_RADIUS,
    CONF_USERNAME,
    DEFAULT_PUBLIC_CAMERAS_RADIUS,
    DOMAIN,
    parse_house_area,
)

PLATFORMS: list[str] = [
    Platform.IMAGE,
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.GEO_LOCATION,
]

NO_SNAPSHOT_PATH = Path(__file__).parent / "assets" / "no_snapshot.png"

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Ucams"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_DOM_URL, msg="Dom url", default="https://dom.ufanet.ru"): str,
    vol.Required(CONF_USERNAME, msg="Username"): str,
    vol.Required(CONF_PASSWORD, msg="Password"): str,
    vol.Required(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, msg="Refresh interval", default=600): int,
    # City cameras are opt-in: they add entities that have nothing to do with
    # the user's own contract, and discovering them costs a cams_server login.
    vol.Optional(CONF_PUBLIC_CAMERAS, msg="City cameras", default=False): bool,
    vol.Optional(CONF_PUBLIC_CAMERAS_QUERY, msg="City cameras search", default=""): str,
    vol.Optional(
        CONF_PUBLIC_CAMERAS_RADIUS,
        msg="City cameras radius (km)",
        default=DEFAULT_PUBLIC_CAMERAS_RADIUS,
    ): vol.Coerce(float),
}

ARCHIVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        # Accepts a unix timestamp (int) OR an ISO datetime / datetime selector
        # value. `int` wins first in vol.Any so legacy automations that pass
        # `(now().timestamp() | int) - 3600` keep working.
        vol.Required("start_time"): vol.Any(int, cv.datetime),
        # Accepts seconds (int) OR a duration-selector dict / timedelta.
        vol.Required("duration"): vol.Any(int, cv.time_period),
    }
)

SNAPSHOT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("filename"): cv.string,
    }
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug("Setting up entry %s", config_entry.entry_id)
    ufanet_api = DomApi(hass, config_entry)
    cameras_api = UcamsApi(hass, config_entry, ufanet_api)
    # Both APIs raise ConfigEntryAuthFailed / ConfigEntryNotReady themselves;
    # let those propagate so HA shows a real error to the user instead of a
    # silent setup failure.
    cameras_info = await cameras_api.get_cameras_info()
    public_cameras_info = await _async_load_public_cameras(hass, config_entry, cameras_api)
    hass.data[config_entry.entry_id] = {
        "cameras_api": cameras_api,
        "dom_api": ufanet_api,
        "cameras_info": cameras_info,
        # City cameras stay in their own bag: only camera/image/geo_location
        # read it, so archive buttons and area assignment can't pick them up.
        "public_cameras_info": public_cameras_info,
    }
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    _assign_areas_by_address(hass, config_entry, cameras_info)
    _async_register_services(hass)
    # Options decide which entities exist (city-camera filters especially), so
    # they only take effect on a reload — do it for the user.
    config_entry.async_on_unload(config_entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(config_entry.entry_id)


async def _async_load_public_cameras(
    hass: HomeAssistant, config_entry: ConfigEntry, cameras_api: UcamsApi
) -> dict:
    """Discover Ufanet's «городские камеры», if the entry opted in.

    Deliberately non-fatal: city cameras are a bonus on top of the user's own
    ones, so a cams_server hiccup must not take the whole entry down with it.
    """
    options = config_entry.options
    if not options.get(CONF_PUBLIC_CAMERAS, False):
        return {}

    query = (options.get(CONF_PUBLIC_CAMERAS_QUERY) or "").strip()
    radius_km = float(options.get(CONF_PUBLIC_CAMERAS_RADIUS) or 0)
    home = (hass.config.latitude, hass.config.longitude)
    if not query and not radius_km:
        _LOGGER.warning(
            "City cameras are on with no search text and no radius — the whole "
            "public list will be considered and truncated. Set one of them."
        )

    try:
        return await cameras_api.get_public_cameras_info(
            query=query or None, radius_km=radius_km or None, home=home
        )
    except ConfigEntryAuthFailed:
        # Credentials going bad is a real failure — HA must start reauth.
        raise
    except Exception as err:
        _LOGGER.warning("Failed to load public city cameras: %s", err)
        return {}


def _assign_areas_by_address(
    hass: HomeAssistant, config_entry: ConfigEntry, cameras_info: dict
) -> None:
    """Group existing devices by parsed street+house and force-assign areas.

    `suggested_area` only fires on first device registration, which doesn't
    help users who already have N devices registered without an area. This
    walks the device registry and pins them to the right area name.
    """
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    for camera_id, cam in cameras_info.items():
        area_name = parse_house_area(cam.get("address"))
        if not area_name:
            continue
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_{camera_id}")}
        )
        if device is None:
            continue
        area = area_reg.async_get_area_by_name(area_name) or area_reg.async_get_or_create(area_name)
        if device.area_id != area.id:
            dev_reg.async_update_device(device.id, area_id=area.id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    res = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if res:
        data = hass.data.pop(config_entry.entry_id)
        await data["cameras_api"].close()
        await data["dom_api"].close()
        if not _ucams_entries(hass):
            hass.services.async_remove(DOMAIN, "snapshot")
            hass.services.async_remove(DOMAIN, "get_archive")
    return res


def _ucams_entries(hass: HomeAssistant) -> list[dict]:
    """Return live ucams entry-data dicts in hass.data."""
    return [d for d in hass.data.values() if isinstance(d, dict) and "cameras_api" in d]


def _find_camera_entity(hass: HomeAssistant, entity_id: str):
    """Look up our Ucams entity by entity_id across all loaded config entries."""
    for entry_data in hass.data.values():
        if not isinstance(entry_data, dict):
            continue
        camera = entry_data.get("camera_entities", {}).get(entity_id)
        if camera is not None:
            return camera
    return None


def _normalize_archive_start(value) -> int:
    """Coerce a service-call start_time to a unix timestamp (int seconds).

    Accepts a plain int (already a timestamp), or a datetime — naive datetimes
    are interpreted as local time, matching what HA's datetime selector sends.
    """
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def _normalize_archive_duration(value) -> int:
    """Coerce a service-call duration to seconds (int)."""
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    return int(value)


def _local_url_for(hass: HomeAssistant, filename: str) -> str | None:
    """Return /local/<…> URL if filename lives under <config>/www/, else None."""
    www_dir = Path(hass.config.path("www")).resolve()
    try:
        rel = Path(filename).resolve().relative_to(www_dir)
    except ValueError:
        return None
    return f"/local/{rel.as_posix()}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "snapshot") and hass.services.has_service(
        DOMAIN, "get_archive"
    ):
        return

    async def handle_snapshot_service(call) -> ServiceResponse:
        entity_id = call.data[ATTR_ENTITY_ID]
        camera = _find_camera_entity(hass, entity_id)
        if camera is None:
            raise HomeAssistantError(f"Camera entity {entity_id} not found")

        filename = call.data.get("filename")
        if not filename:
            slug = entity_id.split(".", 1)[1]
            filename = hass.config.path("www", "ucams_snapshots", f"{slug}_{int(time.time())}.jpg")

        image = None
        try:
            image = await camera.handle_snapshot_from_rtsp()
        except Exception as err:
            _LOGGER.error("Error while getting snapshot: %s", err)

        used_fallback = image is None
        if used_fallback:
            _LOGGER.warning("Falling back to placeholder image for %s", entity_id)
            try:
                image = await hass.async_add_executor_job(NO_SNAPSHOT_PATH.read_bytes)
            except OSError as err:
                raise HomeAssistantError(f"Can't read fallback image: {err}") from err

        def _write_image(to_file: str, image_data: bytes) -> None:
            os.makedirs(os.path.dirname(to_file), exist_ok=True)
            with open(to_file, "wb") as img_file:
                img_file.write(image_data)

        try:
            await hass.async_add_executor_job(_write_image, filename, image)
        except OSError as err:
            raise HomeAssistantError(f"Can't write image to file: {err}") from err

        return {
            "filename": filename,
            "url": _local_url_for(hass, filename),
            "bytes": len(image),
            "used_fallback": used_fallback,
        }

    async def handle_archive_service(call) -> ServiceResponse:
        entity_id = call.data[ATTR_ENTITY_ID]
        camera = _find_camera_entity(hass, entity_id)
        if camera is None:
            raise HomeAssistantError(f"Camera entity {entity_id} not found")
        start_time = _normalize_archive_start(call.data["start_time"])
        duration = _normalize_archive_duration(call.data["duration"])
        data = hass.data[camera.config_entry_id]

        archive_url = await camera.get_camera_archive(start_time, duration)
        if not archive_url:
            raise HomeAssistantError(f"Не удалось получить архив для камеры {entity_id}")

        _LOGGER.info("Получена ссылка на архив: %s", archive_url)
        sensor = data.get("archive_link_sensors", {}).get(camera.camera_id)
        if sensor:
            sensor.update_link(archive_url)

        return {
            "url": archive_url,
            "start_time": start_time,
            "duration": duration,
        }

    hass.services.async_register(
        DOMAIN,
        "get_archive",
        handle_archive_service,
        schema=ARCHIVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "snapshot",
        handle_snapshot_service,
        schema=SNAPSHOT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
