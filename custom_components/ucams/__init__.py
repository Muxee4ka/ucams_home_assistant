import logging
import os
import time
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
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
    CONF_USERNAME,
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
}

ARCHIVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("start_time"): int,  # timestamp в UTC
        vol.Required("duration"): int,  # длительность в секундах
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
    hass.data[config_entry.entry_id] = {
        "cameras_api": cameras_api,
        "dom_api": ufanet_api,
        "cameras_info": cameras_info,
    }
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    _assign_areas_by_address(hass, config_entry, cameras_info)
    _async_register_services(hass)
    return True


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
        area = area_reg.async_get_area_by_name(area_name) or area_reg.async_get_or_create(
            area_name
        )
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
    return [
        d
        for d in hass.data.values()
        if isinstance(d, dict) and "cameras_api" in d
    ]


def _find_camera_entity(hass: HomeAssistant, entity_id: str):
    """Look up our Ucams entity by entity_id across all loaded config entries."""
    for entry_data in hass.data.values():
        if not isinstance(entry_data, dict):
            continue
        camera = entry_data.get("camera_entities", {}).get(entity_id)
        if camera is not None:
            return camera
    return None


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
        start_time = call.data["start_time"]
        duration = call.data["duration"]
        data = hass.data[camera.config_entry_id]

        archive_url = await camera.get_camera_archive(start_time, duration)
        if not archive_url:
            raise HomeAssistantError(f"Не удалось получить архив для камеры {entity_id}")

        _LOGGER.info("Получена ссылка на архив: %s", archive_url)
        sensor = data["archive_link_sensors"].get(camera.camera_id)
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
