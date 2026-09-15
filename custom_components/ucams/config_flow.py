import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import (
    DATA_SCHEMA,
    OPTIONS_SCHEMA,
    PHONE_OPTIONS_SCHEMA,
    PHONE_STEP_SCHEMA,
)
from .ufanet import (
    BASE_URL,
    phone_auth_apply,
    phone_auth_contact_list,
    phone_auth_init,
)
from .utils import (
    AUTH_PASSWORD,
    AUTH_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CONTRACT_ID,
    CONF_DOM_URL,
    CONF_NAME,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# How long we wait for Ufanet to register the incoming call before telling the
# user to try again. Their /apply/ side reports ~70s of validity; a handful of
# short polls keeps the config-flow step responsive without hanging.
_CONTACT_LIST_POLLS = 5
_CONTACT_LIST_DELAY = 2


class UcamsOptionsFlow(OptionsFlowWithConfigEntry):
    async def async_step_init(self, user_input):
        if user_input is not None:
            _LOGGER.debug("OptionsFlow: %s", user_input)
            return self.async_create_entry(
                title=self.config_entry.data["name"],
                data=user_input,
            )

        # Phone accounts have no credentials to edit — only dom url + tuning.
        schema = (
            PHONE_OPTIONS_SCHEMA
            if self.config_entry.data.get(CONF_AUTH_METHOD) == AUTH_PHONE
            else OPTIONS_SCHEMA
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), self.options),
        )


class UcamsConfigFlow(ConfigFlow, domain=DOMAIN):
    # The schema version of the entries that it creates
    # Home Assistant will call your migrate method if the version changes
    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        # Carries state across the multi-step phone flow.
        self._phone_ctx: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> UcamsOptionsFlow:
        return UcamsOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        # Two ways in: the classic contract+password, or the call-auth flow for
        # accounts that have no password (connected via a management company).
        return self.async_show_menu(step_id="user", menu_options=["password", "phone"])

    async def async_step_password(self, user_input=None):
        if user_input is not None:
            _LOGGER.debug("ConfigFlow(password): %s", {**user_input, "password": "***"})
            data = {**user_input, CONF_AUTH_METHOD: AUTH_PASSWORD}
            return self.async_create_entry(title=user_input[CONF_NAME], data=data, options=data)

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(DATA_SCHEMA).extend(OPTIONS_SCHEMA),
        )

    async def async_step_phone(self, user_input=None):
        errors = {}
        if user_input is not None:
            base_url = user_input.get(CONF_DOM_URL) or BASE_URL
            phone = user_input[CONF_PHONE].strip()
            session = async_get_clientsession(self.hass)
            try:
                init = await phone_auth_init(session, base_url, phone)
            except Exception as err:  # surface any failure as a form error
                _LOGGER.warning("phone_auth init failed: %r", err)
                errors["base"] = "phone_init_failed"
            else:
                # Stash the tuning/options the user just entered plus the call
                # handle, to finish once they've placed the call.
                self._phone_ctx = {
                    "options": user_input,
                    "base_url": base_url,
                    "request_id": init["request_id"],
                    "phone_to_call": init["phone_to_call"],
                }
                return await self.async_step_phone_confirm()

        return self.async_show_form(
            step_id="phone",
            data_schema=vol.Schema(PHONE_STEP_SCHEMA),
            errors=errors,
        )

    async def async_step_phone_confirm(self, user_input=None):
        ctx = self._phone_ctx
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            contracts = []
            for _ in range(_CONTACT_LIST_POLLS):
                try:
                    contracts = await phone_auth_contact_list(
                        session, ctx["base_url"], ctx["request_id"]
                    )
                except Exception as err:  # retry on transient error
                    _LOGGER.warning("phone_auth contact_list failed: %r", err)
                    errors["base"] = "phone_call_failed"
                    break
                if contracts:
                    break
                await asyncio.sleep(_CONTACT_LIST_DELAY)

            if not errors:
                if not contracts:
                    errors["base"] = "phone_call_not_detected"
                elif len(contracts) == 1:
                    return await self._finish_phone(contracts[0])
                else:
                    ctx["contracts"] = {str(c["id"]): _contract_label(c) for c in contracts}
                    return await self.async_step_phone_contract()

        return self.async_show_form(
            step_id="phone_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"phone_to_call": ctx.get("phone_to_call", "")},
            errors=errors,
        )

    async def async_step_phone_contract(self, user_input=None):
        ctx = self._phone_ctx
        if user_input is not None:
            contract_id = int(user_input[CONF_CONTRACT_ID])
            return await self._finish_phone({"id": contract_id})

        return self.async_show_form(
            step_id="phone_contract",
            data_schema=vol.Schema({vol.Required(CONF_CONTRACT_ID): vol.In(ctx["contracts"])}),
        )

    async def _finish_phone(self, contract: dict):
        ctx = self._phone_ctx
        contract_id = int(contract["id"])
        session = async_get_clientsession(self.hass)
        try:
            tokens = await phone_auth_apply(
                session, ctx["base_url"], ctx["request_id"], contract_id
            )
        except Exception as err:  # retry on transient error
            _LOGGER.warning("phone_auth apply failed: %r", err)
            return self.async_abort(reason="phone_apply_failed")

        user_opts = ctx["options"]
        options = {k: v for k, v in user_opts.items() if k not in (CONF_NAME, CONF_PHONE)}
        options[CONF_AUTH_METHOD] = AUTH_PHONE
        data = {
            CONF_NAME: user_opts[CONF_NAME],
            CONF_DOM_URL: ctx["base_url"],
            CONF_AUTH_METHOD: AUTH_PHONE,
            CONF_CONTRACT_ID: str(contract_id),
            CONF_ACCESS_TOKEN: tokens["access"],
            CONF_REFRESH_TOKEN: tokens["refresh"],
        }
        return self.async_create_entry(title=user_opts[CONF_NAME], data=data, options=options)


def _contract_label(contract: dict) -> str:
    title = contract.get("title") or str(contract.get("id"))
    addresses = contract.get("addresses") or []
    if addresses:
        views = ", ".join(a.get("string_view", "") for a in addresses if a.get("string_view"))
        if views:
            return f"{title} ({views})"
    return title
