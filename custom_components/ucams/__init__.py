import base64
import logging
import os

import voluptuous as vol
from homeassistant.components.camera import ATTR_FILENAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from custom_components.ucams.sensor import ArchiveLinkSensor
from custom_components.ucams.ucams import UcamsApi
from custom_components.ucams.ufanet import DomApi
from custom_components.ucams.utils import (
    CONF_URL,
    CONF_DOM_URL,
    CONF_NAME,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    DOMAIN
)

PLATFORMS: list[str] = [Platform.IMAGE, Platform.CAMERA, Platform.SWITCH, Platform.SENSOR, Platform.BUTTON]

NO_SNAPSHOT_IMG = "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"

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Ucams"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_DOM_URL, msg="Dom url", default="https://dom.ufanet.ru"): str,
    vol.Required(CONF_USERNAME, msg="Username"): str,
    vol.Required(CONF_PASSWORD, msg="Password"): str,
    vol.Required(
        CONF_CAMERA_IMAGE_REFRESH_INTERVAL, msg="Refresh interval", default=600
    ): int,
}

ARCHIVE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): str,
    vol.Required("start_time"): int,   # timestamp в UTC
    vol.Required("duration"): int,     # длительность в секундах
})

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    try:
        _LOGGER.info(["async_setup_entry", config_entry.entry_id, config_entry.data, config_entry.options])
        ufanet_api = DomApi(hass, config_entry)
        cameras_api = UcamsApi(hass, config_entry, ufanet_api)
        cameras_info = await cameras_api.get_cameras_info()
        hass.data[config_entry.entry_id] = {
            "cameras_api": cameras_api,
            "dom_api": ufanet_api,
            "cameras_info": cameras_info
        }
        await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
        return True
    except Exception as e:
        _LOGGER.error(f"❌ Ошибка загрузки UCAMS: {e}")
        return False


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    res = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if res:
        del hass.data[config_entry.entry_id]
    return res


async def async_setup(hass: HomeAssistant, config_entry: ConfigEntry):
    # Регистрация сервиса для создания снимков

    async def handle_snapshot_service(call):
        entity_id = call.data.get(ATTR_ENTITY_ID)
        if isinstance(entity_id, list):
            entity_id = entity_id[0]
        camera = hass.data["camera"].get_entity(entity_id)
        _LOGGER.debug(f"Entity ID: {entity_id}, Camera: {camera}")
        filename = call.data[ATTR_FILENAME]
        default_image = base64.b64decode(NO_SNAPSHOT_IMG)
        image = None
        try:
            image = await camera.handle_snapshot_from_rtsp()
            if image is None:
                _LOGGER.warning("Can't find fallback image")
        except Exception as e:
            _LOGGER.error("Error %s while getting snapshot.", e)

        image = image if image is not None else default_image

        def _write_image(to_file: str, image_data: bytes) -> None:
            """Executor helper to write image."""
            # Если image_data None, подставляем байты из NO_SNAPSHOT_IMG

            os.makedirs(os.path.dirname(to_file), exist_ok=True)
            with open(to_file, "wb") as img_file:
                img_file.write(image_data)

        try:
            await hass.async_add_executor_job(_write_image, filename, image)
        except OSError as err:
            _LOGGER.error("Can't write image to file: %s", err)

    async def handle_archive_service(call):
        """ Get archive link for camera """
        entity_id = call.data.get(ATTR_ENTITY_ID)
        if isinstance(entity_id, list):
            entity_id = entity_id[0]
        camera = hass.data["camera"].get_entity(entity_id)
        _LOGGER.debug(f"Entity ID: {entity_id}, Camera: {camera}")
        start_time = call.data.get("start_time")
        duration = call.data.get("duration")
        config_entry_id = camera.config_entry_id
        data = hass.data[config_entry_id]

        archive_url = await camera.get_camera_archive(start_time, duration)
        if archive_url:
            _LOGGER.info("Получена ссылка на архив: %s", archive_url)
            sensor = data["archive_link_sensors"].get(camera.camera_id)
            if sensor:
                sensor.update_link(archive_url)
        else:
            _LOGGER.error("Не удалось получить архив для камеры %s", entity_id)

    hass.services.async_register(DOMAIN, "get_archive", handle_archive_service, schema=ARCHIVE_SCHEMA)


    hass.services.async_register(DOMAIN, "snapshot", handle_snapshot_service)
    return True
