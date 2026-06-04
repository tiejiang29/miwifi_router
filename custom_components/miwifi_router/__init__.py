"""MiWiFi Router integration for Home Assistant.

Provides router statistics sensors and per-device tracking with speed data
via the Xiaomi MiWiFi router local API.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant

from .api import MiWiFiAPIClient
from .const import CONF_DEVICE_SCAN_INTERVAL, DEFAULT_DEVICE_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import MiWiFiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MiWiFi Router from a config entry."""
    host = entry.data[CONF_HOST]
    password = entry.data[CONF_PASSWORD]
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    device_scan_interval = entry.options.get(
        CONF_DEVICE_SCAN_INTERVAL, DEFAULT_DEVICE_SCAN_INTERVAL
    )

    # Create API client
    api = MiWiFiAPIClient(host, password)

    # Create coordinator with layered polling
    coordinator = MiWiFiCoordinator(
        hass=hass,
        api=api,
        scan_interval=scan_interval,
        device_scan_interval=device_scan_interval,
    )

    # Store in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Perform first data fetch
    await coordinator.async_config_entry_first_refresh()

    # Set up platforms (sensors + device trackers)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "MiWiFi Router integration set up for %s (scan: %ds, device: %ds)",
        host,
        scan_interval,
        device_scan_interval,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a MiWiFi Router config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: MiWiFiCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.api.close()

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update - reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)
