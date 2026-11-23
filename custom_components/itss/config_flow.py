"""Config flow for I.T.S.S. room thermal controller setup."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_FLOOR_TEMP_ENTITY,
    CONF_HUMIDITY_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PREDICTOR_MODEL,
    CONF_ROOM_NAME,
    CONF_ROOM_TEMP_ENTITY,
    CONF_SCADA_URL,
    CONF_TARGET_TEMP,
    CONF_UPDATE_INTERVAL_SEC,
    DEFAULT_SCADA_URL,
    DEFAULT_TARGET_TEMP,
    DEFAULT_UPDATE_INTERVAL_SEC,
    DOMAIN,
    MODEL_ADAPTIVE_RC,
    MODEL_LINEAR_V12,
)


class ItssConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle UI configuration for each I.T.S.S. managed room."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Show entity mapping form without guessing room entity IDs."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"ITSS - {user_input[CONF_ROOM_NAME]}",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ROOM_NAME): str,
                vol.Required(CONF_ROOM_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_FLOOR_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Optional(CONF_HUMIDITY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_OUTDOOR_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): vol.Coerce(float),
                vol.Required(CONF_PREDICTOR_MODEL, default=MODEL_LINEAR_V12): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=[MODEL_LINEAR_V12, MODEL_ADAPTIVE_RC])
                ),
                vol.Optional(CONF_SCADA_URL, default=DEFAULT_SCADA_URL): str,
                vol.Optional(
                    CONF_UPDATE_INTERVAL_SEC, default=DEFAULT_UPDATE_INTERVAL_SEC
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

