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
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .api import MiWiFiAPIClient
from .const import (
    CONF_DEVICE_SCAN_INTERVAL,
    CONF_FORCE_HASH_ALGO,
    CONF_SPEED_UNIT,
    CONF_TOTAL_UNIT,
    DEFAULT_DEVICE_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SPEED_UNIT_AUTO,
    TOTAL_UNIT_AUTO,
)
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
    force_hash_algo = entry.options.get(CONF_FORCE_HASH_ALGO) or None
    speed_unit = entry.options.get(CONF_SPEED_UNIT, SPEED_UNIT_AUTO)
    total_unit = entry.options.get(CONF_TOTAL_UNIT, TOTAL_UNIT_AUTO)

    # If user changed speed_unit or total_unit, the existing sensor entities
    # need to be removed and re-created so the new native_unit_of_measurement
    # takes effect. HA does not allow changing native_unit on the fly for
    # entities with state_class=TOTAL_INCREASING (long-term stats would break).
    #
    # The last applied unit is stored in entry.options as
    # "_last_applied_speed_unit" / "_last_applied_total_unit". If it differs
    # from the current value, we remove the sensor entities.
    await _remove_sensor_entities_if_unit_changed(hass, entry, speed_unit, total_unit)

    # Create API client with hass instance for non-blocking aiohttp session
    api = MiWiFiAPIClient(host, password, hass=hass, force_hash_algo=force_hash_algo)

    # Create coordinator with layered polling and re-authorization support
    coordinator = MiWiFiCoordinator(
        hass=hass,
        api=api,
        scan_interval=scan_interval,
        device_scan_interval=device_scan_interval,
    )

    # Store in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Perform first data fetch (includes retry with backoff on failure)
    await coordinator.async_config_entry_first_refresh()

    # Set up platforms (sensors + device trackers)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "MiWiFi Router integration set up for %s (scan: %ds, device: %ds, "
        "speed_unit: %s, total_unit: %s)",
        host,
        scan_interval,
        device_scan_interval,
        speed_unit,
        total_unit,
    )

    return True


async def _remove_sensor_entities_if_unit_changed(
    hass: HomeAssistant,
    entry: ConfigEntry,
    current_speed_unit: str,
    current_total_unit: str,
) -> None:
    """Remove sensor entities if speed_unit or total_unit changed.

    This is needed because HA does not allow changing native_unit_of_measurement
    for an existing entity with state_class (it would break long-term stats).
    The only way to apply a new unit is to delete the entity and let the
    platform setup re-create it.

    Side effects:
    - When units change: ALL sensor entities for this config entry are removed
      (including non-speed/non-total ones like cpu_load, online_devices).
      This is because we delete in bulk for simplicity. They will all be
      re-created immediately after by async_forward_entry_setups.
    - State history for these entities is orphaned (lost).
    - Long-term statistics in the statistics table are NOT affected (they
      remain in the DB but no longer linked to the new entities).
    - This only runs when the user actually changes a unit. First-time setup
      and reloads without unit changes are unaffected.

    The last-applied units are tracked in entry.options as
    "_last_applied_speed_unit" / "_last_applied_total_unit".
    """
    last_speed = entry.options.get("_last_applied_speed_unit")
    last_total = entry.options.get("_last_applied_total_unit")

    # On first setup, last_* will be None — record current units but don't remove
    if last_speed is None and last_total is None:
        _LOGGER.debug(
            "MiWiFi Router: first setup, recording initial units speed=%s total=%s",
            current_speed_unit, current_total_unit,
        )
        new_options = {
            **entry.options,
            "_last_applied_speed_unit": current_speed_unit,
            "_last_applied_total_unit": current_total_unit,
        }
        hass.config_entries.async_update_entry(entry, options=new_options)
        return

    # Units unchanged? Nothing to do.
    if last_speed == current_speed_unit and last_total == current_total_unit:
        return

    # Units changed — remove all sensor entities so they get re-created
    _LOGGER.warning(
        "MiWiFi Router: unit changed (speed: %s -> %s, total: %s -> %s). "
        "Removing all sensor entities for re-creation. "
        "NOTE: state history for these entities will be lost.",
        last_speed, current_speed_unit,
        last_total, current_total_unit,
    )

    entity_registry = async_get_entity_registry(hass)
    entities_to_remove: list[str] = []
    for entity_entry in list(entity_registry.entities.values()):
        if (
            entity_entry.config_entry_id == entry.entry_id
            and entity_entry.domain == "sensor"
            and entity_entry.platform == DOMAIN
        ):
            entities_to_remove.append(entity_entry.entity_id)

    for entity_id in entities_to_remove:
        _LOGGER.info("Removing sensor entity for unit change: %s", entity_id)
        entity_registry.async_remove(entity_id)

    # Update last-applied markers
    new_options = {
        **entry.options,
        "_last_applied_speed_unit": current_speed_unit,
        "_last_applied_total_unit": current_total_unit,
    }
    hass.config_entries.async_update_entry(entry, options=new_options)


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
