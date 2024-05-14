import logging
import os

import voluptuous as vol
from homeassistant.components.camera import ATTR_FILENAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from custom_components.ucams.ucams import UcamsApi
from custom_components.ucams.utils import (
    CONF_URL,
    CONF_NAME,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL, DOMAIN
)

PLATFORMS: list[str] = [Platform.IMAGE, Platform.CAMERA]

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Ucams"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_URL, msg="Domain url", default="https://ucams.ufanet.ru"): str,
    vol.Required(CONF_USERNAME, msg="Username"): str,
    vol.Required(CONF_PASSWORD, msg="Password"): str,
    vol.Required(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, msg="Refresh interval", default=60): int,
}

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    _LOGGER.info(["async_setup_entry", config_entry.data, config_entry.options])
    hass.data[config_entry.entry_id] = {
        "cameras_api": UcamsApi(hass, config_entry)
    }
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


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
        camera = hass.data['camera'].get_entity(entity_id)
        filename = call.data[ATTR_FILENAME]

        image = await camera.handle_snapshot_from_ws()

        def _write_image(to_file: str, image_data: bytes) -> None:
            """Executor helper to write image."""
            os.makedirs(os.path.dirname(to_file), exist_ok=True)
            with open(to_file, "wb") as img_file:
                img_file.write(image_data)

        try:
            await hass.async_add_executor_job(_write_image, filename, image)
        except OSError as err:
            _LOGGER.error("Can't write image to file: %s", err)

    hass.services.async_register(DOMAIN, "snapshot", handle_snapshot_service)
    return True
