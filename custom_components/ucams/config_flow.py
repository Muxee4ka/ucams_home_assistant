import logging

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback

from . import DATA_SCHEMA, OPTIONS_SCHEMA
from .utils import DOMAIN

_LOGGER = logging.getLogger(__name__)


class UcamsOptionsFlow(OptionsFlowWithConfigEntry):
    async def async_step_init(self, user_input):
        if user_input is not None:
            _LOGGER.debug("OptionsFlow: %s", user_input)
            return self.async_create_entry(
                title=self.config_entry.data["name"],
                data=user_input,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(OPTIONS_SCHEMA), self.options
            ),
        )


class UcamsConfigFlow(ConfigFlow, domain=DOMAIN):
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> UcamsOptionsFlow:
        return UcamsOptionsFlow(config_entry)

    async def async_step_user(self, user_input):
        if user_input is not None:
            _LOGGER.debug("ConfigFlow: %s", user_input)
            return self.async_create_entry(
                title=user_input["name"], data=user_input, options=user_input
            )

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(DATA_SCHEMA).extend(OPTIONS_SCHEMA)
        )
