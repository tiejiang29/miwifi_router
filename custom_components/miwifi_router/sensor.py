"""Sensor platform for MiWiFi Router.

Provides sensors for:
- Download/Upload speed (configurable unit, default B/s)
- Download/Upload total (configurable unit, default B)
- Online device count
- CPU load (%)
- Memory usage (%)
- Per-device speed/traffic sensors (configurable via Options)

Unit strategy (v1.3.14+):
- User can choose display unit for speed sensors (CONF_SPEED_UNIT) and
  total traffic sensors (CONF_TOTAL_UNIT) via integration options.
- "Auto" (= default) keeps native_unit as B/s and B (legacy v1.3.10 behavior,
  no conversion, max compatibility with long-term stats and Energy Dashboard).
- Other values (kB/s, MB/s, GB/s, KiB/s, MiB/s, GiB/s for speed;
  B, kB, MB, GB, TB, KiB, MiB, GiB, TiB for total) become the new
  native_unit_of_measurement, and native_value is converted from bytes.
- raw_b attribute always preserves the original byte value.
- human_readable attribute provides a friendly string (e.g. "2.45 MB/s").
- When user changes unit, __init__.py removes old sensor entities so
  platform setup re-creates them with the new native_unit. State history
  for those entities will be lost (HA limitation for state_class entities).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
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

from .const import (
    CONF_SPEED_UNIT,
    CONF_TOTAL_UNIT,
    CONF_TRACKED_DEVICES,
    DOMAIN,
    SPEED_UNIT_AUTO,
    SPEED_UNIT_FACTORS,
    TOTAL_UNIT_AUTO,
    TOTAL_UNIT_FACTORS,
)
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


def _convert_value(raw_bytes: float, unit: str) -> float:
    """Convert raw byte value to the target unit.

    Args:
        raw_bytes: original value in bytes (or bytes/sec)
        unit: target unit (e.g. "MB/s", "GB", "KiB", etc.)
              if unit is "auto" or not in factors, returns raw value unchanged

    Returns:
        Converted value (float). Caller should round as needed.
    """
    factor = SPEED_UNIT_FACTORS.get(unit) or TOTAL_UNIT_FACTORS.get(unit)
    if factor is None or factor == 0:
        return raw_bytes
    return raw_bytes / factor


def _round_value(value: float) -> float:
    """Round converted value to reasonable precision.

    For very small values, keep more decimals; for large values, fewer.
    """
    if value == 0:
        return 0
    abs_val = abs(value)
    if abs_val < 0.01:
        return round(value, 6)
    if abs_val < 1:
        return round(value, 4)
    if abs_val < 100:
        return round(value, 3)
    if abs_val < 10_000:
        return round(value, 2)
    return round(value, 1)


# Per-device sensor description templates
# Tuple: (sensor_key, suffix_translation_key, is_speed, icon, state_class)
# - suffix_translation_key: key used to look up translated suffix in strings.json
# - is_speed: True for speed sensors (use CONF_SPEED_UNIT), False for total (use CONF_TOTAL_UNIT)
DEVICE_SENSOR_KEYS: list[tuple[str, str, bool, str, SensorStateClass | None]] = [
    (
        "device_download_speed",
        "download_speed",
        True,
        "mdi:download",
        SensorStateClass.MEASUREMENT,
    ),
    (
        "device_upload_speed",
        "upload_speed",
        True,
        "mdi:upload",
        SensorStateClass.MEASUREMENT,
    ),
    (
        "device_download_total",
        "download_total",
        False,
        "mdi:download-circle",
        SensorStateClass.TOTAL_INCREASING,
    ),
    (
        "device_upload_total",
        "upload_total",
        False,
        "mdi:upload-circle",
        SensorStateClass.TOTAL_INCREASING,
    ),
]


def _load_translations(language: str) -> dict[str, str]:
    """Load translations from translations/ directory for the given language.

    Returns a dict mapping translation_key (e.g. "download_speed") to translated name.
    """
    # Possible file names to try (in order of preference)
    possible_names = [
        f"{language}.json",
        language.replace("-", "_") + ".json",
        language.split("-")[0] + ".json",
    ]
    # Also try fallback to English if the language is not found
    possible_names.append("en.json")

    # Get the component directory
    component_path = Path(__file__).parent
    translations_dir = component_path / "translations"
    if not translations_dir.exists():
        _LOGGER.debug("Translations directory not found at %s", translations_dir)
        return {}

    for name in possible_names:
        file_path = translations_dir / name
        if file_path.exists():
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)
                # Extract entity.sensor.*.name
                result = {}
                entity = data.get("entity", {})
                sensor = entity.get("sensor", {})
                for key, value in sensor.items():
                    if isinstance(value, dict) and "name" in value:
                        result[key] = value["name"]
                if result:
                    _LOGGER.debug("Loaded translations from %s: %s", file_path, result)
                    return result
                else:
                    _LOGGER.debug("No entity.sensor entries found in %s", file_path)
            except Exception as err:
                _LOGGER.debug("Failed to load translations from %s: %s", file_path, err)
                continue
    _LOGGER.debug("No translation file found for language %s", language)
    return {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MiWiFi Router sensors from a config entry."""
    coordinator: MiWiFiCoordinator = hass.data[DOMAIN][entry.entry_id]
    api = coordinator.api

    # Read user-selected units from options
    speed_unit_cfg = entry.options.get(CONF_SPEED_UNIT, SPEED_UNIT_AUTO)
    total_unit_cfg = entry.options.get(CONF_TOTAL_UNIT, TOTAL_UNIT_AUTO)

    # Determine native_unit for speed and total sensors
    # "auto" maps to B/s and B (legacy behavior)
    speed_native_unit: str = (
        UnitOfDataRate.BYTES_PER_SECOND
        if speed_unit_cfg == SPEED_UNIT_AUTO
        else speed_unit_cfg
    )
    total_native_unit: str = (
        UnitOfInformation.BYTES
        if total_unit_cfg == TOTAL_UNIT_AUTO
        else total_unit_cfg
    )

    entities: list[MiWiFiRouterSensor] = []

    # Global sensor descriptions - using translation_key for multi-language support
    descriptions = [
        SensorEntityDescription(
            key="download_speed",
            translation_key="download_speed",
            native_unit_of_measurement=speed_native_unit,
            icon="mdi:download",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="upload_speed",
            translation_key="upload_speed",
            native_unit_of_measurement=speed_native_unit,
            icon="mdi:upload",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="download_total",
            translation_key="download_total",
            native_unit_of_measurement=total_native_unit,
            icon="mdi:download-circle",
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SensorEntityDescription(
            key="upload_total",
            translation_key="upload_total",
            native_unit_of_measurement=total_native_unit,
            icon="mdi:upload-circle",
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        SensorEntityDescription(
            key="online_devices",
            translation_key="online_devices",
            native_unit_of_measurement="devices",
            icon="mdi:devices",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="cpu_load",
            translation_key="cpu_load",
            native_unit_of_measurement=PERCENTAGE,
            icon="mdi:cpu-64-bit",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SensorEntityDescription(
            key="memory_usage",
            translation_key="memory_usage",
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
                speed_unit_cfg=speed_unit_cfg,
                total_unit_cfg=total_unit_cfg,
            )
        )

    async_add_entities(entities)

    # Set up per-device sensors for tracked devices
    device_sensor_manager = MiWiFiDeviceSensorManager(
        hass, coordinator, async_add_entities, entry, api.model, api.firmware,
        speed_unit_cfg, total_unit_cfg,
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
        speed_unit_cfg: str,
        total_unit_cfg: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._model = model
        self._firmware = firmware
        self._speed_unit_cfg = speed_unit_cfg
        self._total_unit_cfg = total_unit_cfg
        self._attr_unique_id = f"{coordinator.api._host}_{description.key}"
        self._attr_extra_state_attributes: dict[str, Any] = {}

        # Copy description attributes to entity attributes
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_icon = description.icon
        self._attr_state_class = description.state_class

        # For speed and total sensors, the state is a string with adaptive units,
        # so we clear the display unit to avoid duplicate units in the UI.
        if description.key in ("download_speed", "upload_speed", "download_total", "upload_total"):
            self._attr_unit_of_measurement = None
        else:
            self._attr_unit_of_measurement = description.native_unit_of_measurement

    @property
    def unit_of_measurement(self) -> str | None:
        """Override unit_of_measurement to ensure None for speed/total sensors."""
        if self.entity_description.key in ("download_speed", "upload_speed", "download_total", "upload_total"):
            return None
        return self._attr_unit_of_measurement

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

    def _convert_for_unit(self, raw_value: float, is_speed: bool) -> float:
        """Convert raw byte value to the configured unit.

        - If unit is "auto", no conversion (return raw bytes).
        - Otherwise, divide by the unit's byte factor.
        """
        unit_cfg = self._speed_unit_cfg if is_speed else self._total_unit_cfg
        if unit_cfg == SPEED_UNIT_AUTO or unit_cfg == TOTAL_UNIT_AUTO:
            return raw_value
        return _round_value(_convert_value(raw_value, unit_cfg))

    @property
    def state(self) -> str | float | None:
        """Return the state.

        For speed/total sensors: return human_readable string (adaptive units).
        For other sensors: return numeric value (unit displayed separately via unit_of_measurement).
        """
        key = self.entity_description.key
        if key in ("download_speed", "upload_speed", "download_total", "upload_total"):
            # Return human_readable if available, otherwise fallback to value+unit
            if self._attr_extra_state_attributes:
                human = self._attr_extra_state_attributes.get("human_readable")
                if human is not None:
                    return human
            # Fallback: return numeric value with unit (should not happen normally)
            if self._attr_native_value is not None and self._attr_native_unit_of_measurement:
                return f"{self._attr_native_value} {self._attr_native_unit_of_measurement}"
            return None
        else:
            # Return numeric value only, unit will be displayed via unit_of_measurement attribute
            return self._attr_native_value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.router_data
        status = data.status

        key = self.entity_description.key

        if key == "download_speed":
            value = status.get("wan", {}).get("downspeed", 0)
            self._attr_native_value = self._convert_for_unit(value, is_speed=True)
            self._attr_extra_state_attributes = {
                "raw_b": value,
                "human_readable": _format_speed(value),
            }

        elif key == "upload_speed":
            value = status.get("wan", {}).get("upspeed", 0)
            self._attr_native_value = self._convert_for_unit(value, is_speed=True)
            self._attr_extra_state_attributes = {
                "raw_b": value,
                "human_readable": _format_speed(value),
            }

        elif key == "download_total":
            value = status.get("wan", {}).get("download", 0)
            self._attr_native_value = self._convert_for_unit(value, is_speed=False)
            self._attr_extra_state_attributes = {
                "raw_b": value,
                "human_readable": _format_bytes(value),
            }

        elif key == "upload_total":
            value = status.get("wan", {}).get("upload", 0)
            self._attr_native_value = self._convert_for_unit(value, is_speed=False)
            self._attr_extra_state_attributes = {
                "raw_b": value,
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
        speed_unit_cfg: str,
        total_unit_cfg: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._entry = entry
        self._model = model
        self._firmware = firmware
        self._speed_unit_cfg = speed_unit_cfg
        self._total_unit_cfg = total_unit_cfg
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

                for (
                    sensor_key,
                    suffix_translation_key,
                    is_speed,
                    icon,
                    state_class,
                ) in DEVICE_SENSOR_KEYS:
                    # Determine native unit based on whether this is speed or total
                    if is_speed:
                        unit_cfg = self._speed_unit_cfg
                        native_unit = (
                            UnitOfDataRate.BYTES_PER_SECOND
                            if unit_cfg == SPEED_UNIT_AUTO
                            else unit_cfg
                        )
                    else:
                        unit_cfg = self._total_unit_cfg
                        native_unit = (
                            UnitOfInformation.BYTES
                            if unit_cfg == TOTAL_UNIT_AUTO
                            else unit_cfg
                        )

                    description = SensorEntityDescription(
                        key=sensor_key,
                        native_unit_of_measurement=native_unit,
                        icon=icon,
                        state_class=state_class,
                    )
                    sensor = MiWiFiDeviceSensor(
                        coordinator=self._coordinator,
                        mac=mac,
                        device_name=device_name,
                        suffix_translation_key=suffix_translation_key,
                        description=description,
                        model=self._model,
                        firmware=self._firmware,
                        unit_cfg=unit_cfg,
                        is_speed=is_speed,
                    )
                    self._known_sensors[mac][sensor_key] = sensor
                    new_entities.append(sensor)

        if new_entities:
            self._async_add_entities(new_entities, update_before_add=True)


class MiWiFiDeviceSensor(CoordinatorEntity[MiWiFiCoordinator], SensorEntity):
    """Per-device speed/traffic sensor.

    Each tracked device gets 4 sensor entities:
    - Device Download Speed (B/s or configured unit, measurement)
    - Device Upload Speed (B/s or configured unit, measurement)
    - Device Download Total (B or configured unit, total_increasing)
    - Device Upload Total (B or configured unit, total_increasing)

    The main state (state) displays a human-readable string with adaptive units
    (e.g. "1.2 MB/s"). The native_value and native_unit_of_measurement remain
    unchanged for long-term statistics.
    """

    # We manually set _attr_name, so keep has_entity_name False
    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: MiWiFiCoordinator,
        mac: str,
        device_name: str,
        suffix_translation_key: str,
        description: SensorEntityDescription,
        model: str,
        firmware: str,
        unit_cfg: str,
        is_speed: bool,
    ) -> None:
        """Initialize the per-device sensor."""
        super().__init__(coordinator)
        self._mac = mac
        self._device_name = device_name
        self._suffix_translation_key = suffix_translation_key
        self.entity_description = description
        self._model = model
        self._firmware = firmware
        self._unit_cfg = unit_cfg
        self._is_speed = is_speed
        self._attr_unique_id = (
            f"{coordinator.api._host}_device_{mac}_{description.key}"
        )
        self._attr_extra_state_attributes: dict[str, Any] = {}

        # Copy description attributes to entity attributes
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_icon = description.icon
        self._attr_state_class = description.state_class

        # For speed and total sensors, the state is a string with adaptive units,
        # so we clear the display unit to avoid duplicate units in the UI.
        self._attr_unit_of_measurement = None

        # Set a temporary English name to avoid None during initialization
        # Will be updated with translation in async_added_to_hass
        suffix_english = suffix_translation_key.replace("_", " ").title()
        self._attr_name = f"{device_name} {suffix_english}"

    @property
    def unit_of_measurement(self) -> None:
        """Override unit_of_measurement to always return None (unit is in state string)."""
        return None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass, set the translated name.

        Priority:
        1. Try to load translation from translations/ directory via file system.
        2. If that fails, fallback to hardcoded Chinese/English mapping.
        """
        await super().async_added_to_hass()
        try:
            # Load translations from file
            translations = _load_translations(self.hass.config.language)
            suffix = translations.get(self._suffix_translation_key)

            if suffix:
                self._attr_name = f"{self._device_name} {suffix}"
                _LOGGER.debug("Device sensor name set via translation file: %s", self._attr_name)
            else:
                # Fallback: hardcoded mapping for Chinese/English
                language = self.hass.config.language
                if language.startswith("zh"):
                    suffix_map = {
                        "download_speed": "下载速率",
                        "upload_speed": "上传速率",
                        "download_total": "累计下载",
                        "upload_total": "累计上传",
                    }
                    suffix = suffix_map.get(self._suffix_translation_key)
                    if suffix:
                        self._attr_name = f"{self._device_name} {suffix}"
                    else:
                        fallback = self._suffix_translation_key.replace("_", " ").title()
                        self._attr_name = f"{self._device_name} {fallback}"
                    _LOGGER.debug("Device sensor name set via Chinese hardcoded mapping: %s", self._attr_name)
                else:
                    # For any other language, use English title
                    fallback = self._suffix_translation_key.replace("_", " ").title()
                    self._attr_name = f"{self._device_name} {fallback}"
                    _LOGGER.debug("Device sensor name set via English fallback: %s", self._attr_name)
        except Exception as err:
            # If anything fails, keep the temporary English name
            _LOGGER.warning("Failed to set translated name for device sensor %s: %s", self.entity_id, err)
        # Write state to update the entity name in the UI
        self.async_write_ha_state()

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

    # Do NOT override 'available' – inherit from CoordinatorEntity,
    # which returns coordinator.last_update_success. This ensures the
    # entity becomes available once the coordinator has fetched data.

    def _convert_for_unit(self, raw_value: float) -> float:
        """Convert raw byte value to the configured unit.

        - If unit is "auto", no conversion (return raw bytes).
        - Otherwise, divide by the unit's byte factor.
        """
        auto_value = SPEED_UNIT_AUTO if self._is_speed else TOTAL_UNIT_AUTO
        if self._unit_cfg == auto_value:
            return raw_value
        return _round_value(_convert_value(raw_value, self._unit_cfg))

    @property
    def state(self) -> str | None:
        """Return the state as a human-readable string with adaptive units."""
        if self._attr_extra_state_attributes:
            human = self._attr_extra_state_attributes.get("human_readable")
            if human is not None:
                return human
        # Fallback: return numeric value with unit (should not happen)
        if self._attr_native_value is not None and self._attr_native_unit_of_measurement:
            return f"{self._attr_native_value} {self._attr_native_unit_of_measurement}"
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        devices = self.coordinator.router_data.devices
        if self._mac in devices:
            dev_data = devices[self._mac]
            key = self.entity_description.key

            if key == "device_download_speed":
                value = int(dev_data.get("downspeed", 0))
                self._attr_native_value = self._convert_for_unit(value)
                self._attr_extra_state_attributes = {
                    "raw_b": value,
                    "human_readable": _format_speed(value),
                }

            elif key == "device_upload_speed":
                value = int(dev_data.get("upspeed", 0))
                self._attr_native_value = self._convert_for_unit(value)
                self._attr_extra_state_attributes = {
                    "raw_b": value,
                    "human_readable": _format_speed(value),
                }

            elif key == "device_download_total":
                value = int(dev_data.get("download", 0))
                self._attr_native_value = self._convert_for_unit(value)
                self._attr_extra_state_attributes = {
                    "raw_b": value,
                    "human_readable": _format_bytes(value),
                }

            elif key == "device_upload_total":
                value = int(dev_data.get("upload", 0))
                self._attr_native_value = self._convert_for_unit(value)
                self._attr_extra_state_attributes = {
                    "raw_b": value,
                    "human_readable": _format_bytes(value),
                }

        self.async_write_ha_state()
