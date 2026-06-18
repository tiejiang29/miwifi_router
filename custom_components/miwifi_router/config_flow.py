"""Config flow for MiWiFi Router integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .api import MiWiFiAPIClient, MiWiFiAuthError, MiWiFiConnectionError
from .const import (
    CONF_DEVICE_SCAN_INTERVAL,
    CONF_FORCE_HASH_ALGO,
    CONF_SPEED_UNIT,
    CONF_TOTAL_UNIT,
    CONF_TRACKED_DEVICES,
    DEFAULT_DEVICE_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SPEED_UNIT_AUTO,
    SPEED_UNIT_OPTIONS,
    TOTAL_UNIT_AUTO,
    TOTAL_UNIT_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


class MiWiFiRouterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MiWiFi Router."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._host: str = ""
        self._password: str = ""
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._device_scan_interval: int = DEFAULT_DEVICE_SCAN_INTERVAL
        self._device_names: dict[str, str] = {}
        self._device_options: dict[str, str] = {}
        self._force_hash_algo: str | None = None
        self._speed_unit: str = SPEED_UNIT_AUTO
        self._total_unit: str = TOTAL_UNIT_AUTO

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
            force_hash_algo = user_input.get(CONF_FORCE_HASH_ALGO) or None
            speed_unit = user_input.get(CONF_SPEED_UNIT, SPEED_UNIT_AUTO) or SPEED_UNIT_AUTO
            total_unit = user_input.get(CONF_TOTAL_UNIT, TOTAL_UNIT_AUTO) or TOTAL_UNIT_AUTO

            # Check if already configured
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            # Test connection with hass for non-blocking aiohttp
            api = MiWiFiAPIClient(
                host, password, hass=self.hass, force_hash_algo=force_hash_algo
            )
            try:
                await api.test_connection()

                # Store for next step
                self._host = host
                self._password = password
                self._scan_interval = scan_interval
                self._device_scan_interval = device_scan_interval
                self._force_hash_algo = force_hash_algo
                self._speed_unit = speed_unit
                self._total_unit = total_unit

                # Fetch device list for device selection step
                try:
                    device_list = await api.get_device_list()
                    self._device_options = {}
                    self._device_names = {}
                    for dev in device_list.get("dev", []):
                        mac = dev.get("mac", "")
                        if not mac:
                            continue
                        name = (
                            dev.get("hostname", "")
                            or dev.get("name", "")
                            or mac
                        )
                        if not name or name.upper() == mac.upper():
                            name = f"Device {mac}"
                        display = f"{name} ({mac})"
                        self._device_options[mac] = display
                        self._device_names[mac] = name
                except Exception:
                    _LOGGER.debug("Failed to fetch device list during setup")

                # Go to device selection step (or skip if no devices found)
                if self._device_options:
                    return await self.async_step_devices()

                # No devices found, create entry directly
                return self._create_entry_with_tracked({})

            except MiWiFiAuthError as err:
                _LOGGER.warning("Authentication failed for %s: %s", host, err)
                errors["base"] = "invalid_auth"
            except MiWiFiConnectionError as err:
                _LOGGER.warning("Connection failed for %s: %s", host, err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception for %s: %s", host, err)
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
                vol.Optional(
                    CONF_FORCE_HASH_ALGO, default=""
                ): vol.In({
                    "": "自动检测（推荐）",
                    "SHA1": "强制 SHA1（老固件：AX3600、AC2100、AX9000 等）",
                    "SHA256": "强制 SHA256（新固件：BE5000、BE3600、小米路由器 7000 等）",
                }),
                vol.Optional(
                    CONF_SPEED_UNIT, default=SPEED_UNIT_AUTO
                ): vol.In(SPEED_UNIT_OPTIONS),
                vol.Optional(
                    CONF_TOTAL_UNIT, default=TOTAL_UNIT_AUTO
                ): vol.In(TOTAL_UNIT_OPTIONS),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device selection step."""
        if user_input is not None:
            selected_macs: list[str] = list(
                user_input.get(CONF_TRACKED_DEVICES, [])
            )
            tracked_devices: dict[str, str] = {}
            for mac in selected_macs:
                tracked_devices[mac] = self._device_names.get(mac, mac)
            return self._create_entry_with_tracked(tracked_devices)

        schema = vol.Schema({
            vol.Optional(CONF_TRACKED_DEVICES, default=[]): cv.multi_select(
                self._device_options
            ),
        })

        return self.async_show_form(step_id="devices", data_schema=schema)

    def _create_entry_with_tracked(
        self, tracked_devices: dict[str, str]
    ) -> FlowResult:
        """Create the config entry with tracked devices."""
        # Get display name
        api_display = self._host
        # We can't easily get model here without another API call,
        # so use host as fallback
        return self.async_create_entry(
            title=f"MiWiFi Router ({self._host})",
            data={
                CONF_HOST: self._host,
                CONF_PASSWORD: self._password,
            },
            options={
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_DEVICE_SCAN_INTERVAL: self._device_scan_interval,
                CONF_TRACKED_DEVICES: tracked_devices,
                CONF_FORCE_HASH_ALGO: self._force_hash_algo or "",
                CONF_SPEED_UNIT: self._speed_unit,
                CONF_TOTAL_UNIT: self._total_unit,
            },
        )


class MiWiFiRouterOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for MiWiFi Router."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._device_names: dict[str, str] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            # Build tracked devices dict from multi-select results
            selected_macs: list[str] = list(
                user_input.get(CONF_TRACKED_DEVICES, [])
            )
            tracked_devices: dict[str, str] = {}
            for mac in selected_macs:
                tracked_devices[mac] = self._device_names.get(mac, mac)

            data = {
                CONF_SCAN_INTERVAL: user_input.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                CONF_DEVICE_SCAN_INTERVAL: user_input.get(
                    CONF_DEVICE_SCAN_INTERVAL, DEFAULT_DEVICE_SCAN_INTERVAL
                ),
                CONF_TRACKED_DEVICES: tracked_devices,
                CONF_FORCE_HASH_ALGO: user_input.get(CONF_FORCE_HASH_ALGO, "") or "",
                CONF_SPEED_UNIT: user_input.get(CONF_SPEED_UNIT, SPEED_UNIT_AUTO) or SPEED_UNIT_AUTO,
                CONF_TOTAL_UNIT: user_input.get(CONF_TOTAL_UNIT, TOTAL_UNIT_AUTO) or TOTAL_UNIT_AUTO,
            }
            return self.async_create_entry(title="", data=data)

        # Build device multi-select options from coordinator data
        device_options: dict[str, str] = {}
        self._device_names = {}

        coordinator = self.hass.data.get(DOMAIN, {}).get(
            self._config_entry.entry_id
        )
        if coordinator and coordinator.router_data.devices:
            for mac, dev_data in coordinator.router_data.devices.items():
                name = (
                    dev_data.get("hostname", "")
                    or dev_data.get("name", "")
                    or mac
                )
                if not name or name.upper() == mac.upper():
                    name = f"Device {mac}"
                display = f"{name} ({mac})"
                device_options[mac] = display
                self._device_names[mac] = name

        # Include previously tracked devices that may be offline now
        prev_tracked = self._config_entry.options.get(CONF_TRACKED_DEVICES, {})
        if isinstance(prev_tracked, dict):
            for mac, name in prev_tracked.items():
                if mac not in device_options:
                    display = f"{name} ({mac}) [离线]"
                    device_options[mac] = display
                    self._device_names[mac] = name

        # Default selection = previously tracked devices
        default_selected: list[str] = (
            list(prev_tracked.keys()) if isinstance(prev_tracked, dict) else []
        )

        # Build schema with or without device selection
        schema_dict: dict[Any, Any] = {
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
            vol.Optional(
                CONF_FORCE_HASH_ALGO,
                default=self._config_entry.options.get(CONF_FORCE_HASH_ALGO, ""),
            ): vol.In({
                "": "自动检测（推荐）",
                "SHA1": "强制 SHA1（老固件：AX3600、AC2100、AX9000 等）",
                "SHA256": "强制 SHA256（新固件：BE5000、BE3600、小米路由器 7000 等）",
            }),
            vol.Optional(
                CONF_SPEED_UNIT,
                default=self._config_entry.options.get(CONF_SPEED_UNIT, SPEED_UNIT_AUTO),
            ): vol.In(SPEED_UNIT_OPTIONS),
            vol.Optional(
                CONF_TOTAL_UNIT,
                default=self._config_entry.options.get(CONF_TOTAL_UNIT, TOTAL_UNIT_AUTO),
            ): vol.In(TOTAL_UNIT_OPTIONS),
        }

        if device_options:
            schema_dict[
                vol.Optional(
                    CONF_TRACKED_DEVICES, default=default_selected
                )
            ] = cv.multi_select(device_options)

        schema = vol.Schema(schema_dict)

        return self.async_show_form(step_id="init", data_schema=schema)
