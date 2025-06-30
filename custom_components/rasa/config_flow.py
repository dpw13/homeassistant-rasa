"""Config flow for the Rasa NLP integration."""

# pylint: disable=fixme

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_ACTION_PORT, CONF_SERVER_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# TODO adjust the data schema to the data that you need
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_SERVER_URL, description={"suggested_value": "http://localhost:5005"}
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Required(CONF_ACTION_PORT, description={"suggested_value": 5055}): int,
    }
)


class PlaceholderHub:
    """Placeholder class to make tests pass.

    TODO Remove this placeholder class and replace with things from your PyPI package.
    """

    def __init__(self, url: str, port: int) -> None:
        """Initialize."""
        self.url = url
        self.port = port

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        return True


class RasaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rasa NLP."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self.url: str | None = None
        self.port: int | None = 5056
        self.client: Any = None

    async def validate_input(self) -> dict[str, Any]:
        """Validate the user input allows us to connect."""
        if self.url is None or self.port is None:
            raise CannotConnect

        hub = PlaceholderHub(self.url, self.port)

        # If you cannot connect:
        # throw CannotConnect
        # If the authentication is wrong:
        # InvalidAuth
        if not await hub.authenticate():
            raise InvalidAuth

        # Return info that you want to store in the config entry.
        return {"title": "Rasa Server"}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        user_input = user_input or {}
        self.url = user_input.get(CONF_SERVER_URL, self.url)
        self.port = user_input.get(CONF_ACTION_PORT, self.port)

        errors: dict[str, str] = {}
        if user_input:
            try:
                info = await self.validate_input()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=info["title"],
                    data={CONF_SERVER_URL: self.url, CONF_ACTION_PORT: self.port},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
