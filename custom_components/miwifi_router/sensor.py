"""Sensor platform for MiWiFi Router.

Provides sensors for:
- Download/Upload speed (B/s)
- Download/Upload total (B)
- Online device count
- CPU load (%)
- Memory usage (%)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MiWiFiCoordinator

_LOGGER = logging.getLogger(__name__)


def _format_speed(speed_bytes: float) -> str:
    """Format speed value for display in attributes."""
    if speed_bytes >= 1_000_000:
        return f"{speed_bytes / 1_000_000:.2f} MB/s"
    if speed_bytes >= 1_000:
        return f"{speed_bytes / 1_000:.2f} KB/s"
    return f"{speed_bytes:.0f} B/s"


def _format_bytes(total_bytes: float) -> str:
    """Format total bytes for display in attributes."""
    if total_bytes >= 1_000_000_000_000:
        return f"{total_bytes / 1_000_000_000_000:.2f} TB"
    if total_bytes >= 1_000_000_000:
        return f"{total_bytes / 1_000_000_000:.2f} GB"
    if total_bytes >= 1_000_000:
        return f"{total_bytes / 1_000_000:.2f} MB"
    if total_bytes >= 1_000:
        return f"{total_bytes / 1_000:.2f} KB"
    return f"{total_bytes:.0f} B"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MiWiFi Router sensors from a config entry."""
    coordinator: MiWiFiCoordinator = hass.data[DOMAIN][entry.entry_id]
    api = coordinator.api

    entities: list[MiWiFiRouterSensor] = []

    descriptions = [
        SensorEntityDescription(
            key="download_speed",
            name="Download Speed",
            native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
            suggested_unit_of_measurement=UnitOfDataRate.MEGABYTES_PER_SECOND,
            suggested_display_precision=2,
            icon="mdi:download",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="upload_speed",
            name="Upload Speed",
            native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
            suggested_unit_of_measurement=UnitOfDataRate.MEGABYTES_PER_SECOND,
            suggested_display_precision=2,
            icon="mdi:upload",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="download_total",
            name="Download Total",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            icon="mdi:download-circle",
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SensorEntityDescription(
            key="upload_total",
            name="Upload Total",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            icon="mdi:upload-circle",
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SensorEntityDescription(
            key="online_devices",
            name="Online Devices",
            native_unit_of_measurement="devices",
            icon="mdi:devices",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="cpu_load",
            name="CPU Load",
            native_unit_of_measurement=PERCENTAGE,
            icon="mdi:cpu-64-bit",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="memory_usage",
            name="Memory Usage",
            native_unit_of_measurement=PERCENTAGE,
            icon="mdi:memory",
            state_class=SensorStateClass.MEASUREMENT,
        ),
    ]

    for description in descriptions:
        entities.append(
            MiWiFiRouterSensor(
                coordinator=coordinator,
                description=description,
                model=api.model,
                firmware=api.firmware,
            )
        )

    async_add_entities(entities)


class MiWiFiRouterSensor(CoordinatorEntity[MiWiFiCoordinator], SensorEntity):
    """Representation of a MiWiFi Router sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MiWiFiCoordinator,
        description: SensorEntityDescription,
        model: str,
        firmware: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._model = model
        self._firmware = firmware
        self._attr_unique_id = f"{coordinator.api._host}_{description.key}"
        self._attr_extra_state_attributes: dict[str, Any] = {}

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for the router."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.api._host)},
            "name": self._model or "MiWiFi Router",
            "manufacturer": "Xiaomi",
            "model": self._model,
            "sw_version": self._firmware,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.router_data
        status = data.status

        key = self.entity_description.key

        if key == "download_speed":
            value = status.get("wan", {}).get("downspeed", 0)
            self._attr_native_value = value
            self._attr_extra_state_attributes = {
                "human_readable": _format_speed(value),
            }

        elif key == "upload_speed":
            value = status.get("wan", {}).get("upspeed", 0)
            self._attr_native_value = value
            self._attr_extra_state_attributes = {
                "human_readable": _format_speed(value),
            }

        elif key == "download_total":
            value = status.get("wan", {}).get("download", 0)
            self._attr_native_value = value
            self._attr_extra_state_attributes = {
                "human_readable": _format_bytes(value),
            }

        elif key == "upload_total":
            value = status.get("wan", {}).get("upload", 0)
            self._attr_native_value = value
            self._attr_extra_state_attributes = {
                "human_readable": _format_bytes(value),
            }

        elif key == "online_devices":
            online = status.get("count", {}).get("online", 0)
            total = status.get("count", {}).get("all", 0)
            self._attr_native_value = online
            self._attr_extra_state_attributes = {
                "total_devices": total,
                "offline_devices": max(0, total - online),
            }

        elif key == "cpu_load":
            cpu = status.get("cpu", {})
            load = cpu.get("load", 0)
            # CPU load is a ratio (0-1), convert to percentage
            if isinstance(load, (int, float)) and 0 < load <= 1:
                load = round(load * 100, 1)
            elif isinstance(load, (int, float)) and load > 1:
                load = round(load, 1)
            self._attr_native_value = load
            self._attr_extra_state_attributes = {
                "cores": cpu.get("core", 0),
                "frequency": cpu.get("hz", ""),
            }

        elif key == "memory_usage":
            mem = status.get("mem", {})
            usage = mem.get("usage", 0)
            # Memory usage is a ratio (0-1), convert to percentage
            if isinstance(usage, (int, float)) and 0 < usage <= 1:
                usage = round(usage * 100, 1)
            elif isinstance(usage, (int, float)) and usage > 1:
                usage = round(usage, 1)
            self._attr_native_value = usage
            self._attr_extra_state_attributes = {
                "total_memory": mem.get("total", ""),
            }

        self.async_write_ha_state()
