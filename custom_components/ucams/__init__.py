import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from custom_components.ucams.ucams import UcamsApi
from custom_components.ucams.utils import (
    CONF_NAME,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL
)

PLATFORMS: list[str] = [Platform.IMAGE, Platform.CAMERA]

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Ucams"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_USERNAME, msg="Username"): str,
    vol.Required(CONF_PASSWORD, msg="Password"): str,
    vol.Required(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, msg="Refresh interval", default=2): int,
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
