"""Sensor platform for MiWiFi Router.

Provides sensors for:
- Download/Upload speed (B/s)
- Download/Upload total (B)
- Online device count
- CPU load (%)
- Memory usage (%)
- Per-device speed/traffic sensors (configurable via Options)
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
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TRACKED_DEVICES, DOMAIN
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


# Per-device sensor description templates (key, name suffix, unit, icon, state_class)
DEVICE_SENSOR_KEYS: list[tuple[str, str, str, str, SensorStateClass | None]] = [
    (
        "device_download_speed",
        "Download Speed",
        UnitOfDataRate.BYTES_PER_SECOND,
        "mdi:download",
        SensorStateClass.MEASUREMENT,
    ),
    (
        "device_upload_speed",
        "Upload Speed",
        UnitOfDataRate.BYTES_PER_SECOND,
        "mdi:upload",
        SensorStateClass.MEASUREMENT,
    ),
    (
        "device_download_total",
        "Download Total",
        UnitOfInformation.BYTES,
        "mdi:download-circle",
        SensorStateClass.TOTAL_INCREASING,
    ),
    (
        "device_upload_total",
        "Upload Total",
        UnitOfInformation.BYTES,
        "mdi:upload-circle",
        SensorStateClass.TOTAL_INCREASING,
    ),
]


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
            icon="mdi:download",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="upload_speed",
            name="Upload Speed",
            native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
            icon="mdi:upload",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="download_total",
            name="Download Total",
            native_unit_of_measurement=UnitOfInformation.BYTES,
            icon="mdi:download-circle",
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SensorEntityDescription(
            key="upload_total",
            name="Upload Total",
            native_unit_of_measurement=UnitOfInformation.BYTES,
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

    # Set up per-device sensors for tracked devices
    device_sensor_manager = MiWiFiDeviceSensorManager(
        hass, coordinator, async_add_entities, entry, api.model, api.firmware
    )

    # Register a listener to update device sensors when coordinator data changes
    entry.async_on_unload(
        coordinator.async_add_listener(device_sensor_manager.update_sensors)
    )

    # Clean up entity registry for untracked device sensors
    await _cleanup_untracked_device_sensors(hass, entry, coordinator)

    # Initial setup with current data
    device_sensor_manager.update_sensors()


async def _cleanup_untracked_device_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MiWiFiCoordinator,
) -> None:
    """Remove entity registry entries for device sensors that are no longer tracked."""
    tracked_devices: dict[str, str] = entry.options.get(CONF_TRACKED_DEVICES, {})
    if not isinstance(tracked_devices, dict):
        tracked_devices = {}

    host = coordinator.api._host

    # Build set of expected unique_ids for currently tracked device sensors
    expected_unique_ids: set[str] = set()
    for mac in tracked_devices:
        for key, _, _, _, _ in DEVICE_SENSOR_KEYS:
            expected_unique_ids.add(f"{host}_device_{mac}_{key}")

    # Find and remove entities that belong to untracked devices
    entity_registry = async_get_entity_registry(hass)
    entities_to_remove: list[str] = []

    for entity_entry in entity_registry.entities.values():
        if (
            entity_entry.config_entry_id == entry.entry_id
            and entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{host}_device_")
            and entity_entry.unique_id not in expected_unique_ids
        ):
            entities_to_remove.append(entity_entry.entity_id)

    for entity_id in entities_to_remove:
        _LOGGER.info(
            "Removing untracked device sensor entity: %s", entity_id
        )
        entity_registry.async_remove(entity_id)


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


class MiWiFiDeviceSensorManager:
    """Manages per-device sensor entities based on tracked_devices config.

    Only devices selected in the Options flow will have sensor entities created.
    Each tracked device gets 4 sensors: download_speed, upload_speed,
    download_total, upload_total.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: MiWiFiCoordinator,
        async_add_entities: AddEntitiesCallback,
        entry: ConfigEntry,
        model: str,
        firmware: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._entry = entry
        self._model = model
        self._firmware = firmware
        # MAC → {sensor_key: MiWiFiDeviceSensor}
        self._known_sensors: dict[str, dict[str, MiWiFiDeviceSensor]] = {}

    def _get_tracked_devices(self) -> dict[str, str]:
        """Get tracked devices from config entry options.

        Returns dict of {mac: device_name}.
        """
        tracked = self._entry.options.get(CONF_TRACKED_DEVICES, {})
        if isinstance(tracked, dict):
            return tracked
        return {}

    def update_sensors(self) -> None:
        """Create/update per-device sensors based on config and coordinator data."""
        tracked_devices = self._get_tracked_devices()

        new_entities: list[MiWiFiDeviceSensor] = []

        for mac, device_name in tracked_devices.items():
            if mac not in self._known_sensors:
                # Create sensors for this tracked device
                self._known_sensors[mac] = {}

                for key, name_suffix, unit, icon, state_class in DEVICE_SENSOR_KEYS:
                    description = SensorEntityDescription(
                        key=key,
                        name=f"{device_name} {name_suffix}",
                        native_unit_of_measurement=unit,
                        icon=icon,
                        state_class=state_class,
                    )
                    sensor = MiWiFiDeviceSensor(
                        coordinator=self._coordinator,
                        mac=mac,
                        device_name=device_name,
                        description=description,
                        model=self._model,
                        firmware=self._firmware,
                    )
                    self._known_sensors[mac][key] = sensor
                    new_entities.append(sensor)

        if new_entities:
            self._async_add_entities(new_entities, update_before_add=True)


class MiWiFiDeviceSensor(CoordinatorEntity[MiWiFiCoordinator], SensorEntity):
    """Per-device speed/traffic sensor.

    Each tracked device gets 4 sensor entities:
    - Device Download Speed (B/s, measurement)
    - Device Upload Speed (B/s, measurement)
    - Device Download Total (B, total_increasing)
    - Device Upload Total (B, total_increasing)
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MiWiFiCoordinator,
        mac: str,
        device_name: str,
        description: SensorEntityDescription,
        model: str,
        firmware: str,
    ) -> None:
        """Initialize the per-device sensor."""
        super().__init__(coordinator)
        self._mac = mac
        self._device_name = device_name
        self.entity_description = description
        self._model = model
        self._firmware = firmware
        self._attr_unique_id = (
            f"{coordinator.api._host}_device_{mac}_{description.key}"
        )
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

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Device sensors are available even when the device is offline,
        as long as the coordinator update was successful (we keep
        the last known value).
        """
        return self.coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        devices = self.coordinator.router_data.devices
        if self._mac in devices:
            dev_data = devices[self._mac]
            key = self.entity_description.key

            if key == "device_download_speed":
                value = int(dev_data.get("downspeed", 0))
                self._attr_native_value = value
                self._attr_extra_state_attributes = {
                    "human_readable": _format_speed(value),
                }

            elif key == "device_upload_speed":
                value = int(dev_data.get("upspeed", 0))
                self._attr_native_value = value
                self._attr_extra_state_attributes = {
                    "human_readable": _format_speed(value),
                }

            elif key == "device_download_total":
                value = int(dev_data.get("download", 0))
                self._attr_native_value = value
                self._attr_extra_state_attributes = {
                    "human_readable": _format_bytes(value),
                }

            elif key == "device_upload_total":
                value = int(dev_data.get("upload", 0))
                self._attr_native_value = value
                self._attr_extra_state_attributes = {
                    "human_readable": _format_bytes(value),
                }

        self.async_write_ha_state()
