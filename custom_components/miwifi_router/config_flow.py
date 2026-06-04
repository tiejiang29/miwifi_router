"""Config flow for MiWiFi Router integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api import MiWiFiAPIClient, MiWiFiAuthError, MiWiFiConnectionError
from .const import (
    CONF_DEVICE_SCAN_INTERVAL,
    DEFAULT_DEVICE_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class MiWiFiRouterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MiWiFi Router."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MiWiFiRouterOptionsFlow:
        """Get the options flow for this handler."""
        return MiWiFiRouterOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            password = user_input[CONF_PASSWORD]
            scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            device_scan_interval = user_input.get(
                CONF_DEVICE_SCAN_INTERVAL, DEFAULT_DEVICE_SCAN_INTERVAL
            )

            # Check if already configured
            await self.async_set_unique_id(host)
            self._abort_if_already_configured()

            # Test connection
            api = MiWiFiAPIClient(host, password)
            try:
                success = await api.test_connection()
                if success:
                    # Get model info for the entry title
                    model = api.model
                    try:
                        init_info = await api.get_init_info()
                        display_name = init_info.get("hardware", {}).get(
                            "displayName", model
                        )
                    except Exception:
                        display_name = model

                    return self.async_create_entry(
                        title=f"{display_name} ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PASSWORD: password,
                        },
                        options={
                            CONF_SCAN_INTERVAL: scan_interval,
                            CONF_DEVICE_SCAN_INTERVAL: device_scan_interval,
                        },
                    )
                else:
                    errors["base"] = "cannot_connect"
            except MiWiFiAuthError:
                errors["base"] = "invalid_auth"
            except MiWiFiConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            finally:
                await api.close()

        # Show the form
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): int,
                vol.Optional(
                    CONF_DEVICE_SCAN_INTERVAL, default=DEFAULT_DEVICE_SCAN_INTERVAL
                ): int,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )


class MiWiFiRouterOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for MiWiFi Router."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self._config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                ): int,
                vol.Optional(
                    CONF_DEVICE_SCAN_INTERVAL,
                    default=self._config_entry.options.get(
                        CONF_DEVICE_SCAN_INTERVAL, DEFAULT_DEVICE_SCAN_INTERVAL
                    ),
                ): int,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
