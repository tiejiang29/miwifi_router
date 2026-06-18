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
    DEFAULT_DEVICE_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SENSOR_UNIT_MIGRATED,
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

    # One-time migration (v1.3.12): remove old sensor entities so they get
    # re-created with the new suggested_unit_of_measurement added in v1.3.11.
    # HA's suggested_unit_of_measurement only applies on initial entity
    # creation — existing entities created before v1.3.11 won't pick up the
    # new suggested unit unless we delete and recreate them.
    # Idempotent: skipped if SENSOR_UNIT_MIGRATED marker is already set.
    if not entry.options.get(SENSOR_UNIT_MIGRATED):
        await _migrate_sensor_entities_for_suggested_unit(hass, entry)

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
        "MiWiFi Router integration set up for %s (scan: %ds, device: %ds)",
        host,
        scan_interval,
        device_scan_interval,
    )

    return True


async def _migrate_sensor_entities_for_suggested_unit(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove existing sensor entities so they get re-created with new settings.

    This is needed because HA's suggested_unit_of_measurement only applies
    on initial entity creation. Old sensor entities created before v1.3.11
    don't have the suggested_unit, so they display in B/s or B instead of
    the more readable MB/s or GB.

    Side effects:
    - sensor.* entities matching this config entry are removed from the
      entity registry and will be re-created on the next platform setup.
    - Historical state data for these entities (in the states table) will
      no longer be associated with the new entities. Long-term statistics
      in the statistics table may also become orphaned.
    - This only runs ONCE per config entry (guarded by SENSOR_UNIT_MIGRATED
      marker in entry.options).
    """
    entity_registry = async_get_entity_registry(hass)
    entities_to_remove: list[str] = []

    for entity_entry in list(entity_registry.entities.values()):
        if (
            entity_entry.config_entry_id == entry.entry_id
            and entity_entry.domain == "sensor"
            and entity_entry.platform == DOMAIN
        ):
            entities_to_remove.append(entity_entry.entity_id)

    if entities_to_remove:
        _LOGGER.info(
            "MiWiFi Router: migrating %d sensor entities for v1.3.11 unit display "
            "(removing old entities, they will be re-created with suggested_unit)",
            len(entities_to_remove),
        )
        for entity_id in entities_to_remove:
            _LOGGER.debug("Removing entity for unit migration: %s", entity_id)
            entity_registry.async_remove(entity_id)
    else:
        _LOGGER.debug(
            "MiWiFi Router: no sensor entities to migrate for %s", entry.entry_id
        )

    # Mark migration as done so it doesn't run again
    new_options = {**entry.options, SENSOR_UNIT_MIGRATED: True}
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
