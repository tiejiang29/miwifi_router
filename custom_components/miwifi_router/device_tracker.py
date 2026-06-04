"""Device tracker platform for MiWiFi Router.

Provides device online/offline detection and per-device speed monitoring.
Each connected device appears as a device_tracker entity with speed attributes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import (
    SourceType,
    TrackerEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MiWiFiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MiWiFi Router device trackers from a config entry."""
    coordinator: MiWiFiCoordinator = hass.data[DOMAIN][entry.entry_id]
    api = coordinator.api

    # Dynamic entity approach: a tracker manager adds trackers as devices appear
    tracker_manager = MiWiFiTrackerManager(
        hass, coordinator, async_add_entities, api.model, api.firmware
    )

    # Register a listener to update device trackers when coordinator data changes
    entry.async_on_unload(
        coordinator.async_add_listener(tracker_manager.update_devices)
    )

    # Initial setup with current data
    tracker_manager.update_devices()


class MiWiFiTrackerManager:
    """Manages dynamic device tracker entities.

    New devices are added as they appear; offline devices stay in the registry
    as 'not_home' so that automations can still trigger on state changes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: MiWiFiCoordinator,
        async_add_entities: AddEntitiesCallback,
        model: str,
        firmware: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._model = model
        self._firmware = firmware
        self._known_devices: dict[str, MiWiFiDeviceTracker] = {}

    def update_devices(self) -> None:
        """Update device trackers based on latest coordinator data."""
        data = self._coordinator.router_data
        devices = data.devices

        new_entities: list[MiWiFiDeviceTracker] = []

        for mac, dev_data in devices.items():
            if mac in self._known_devices:
                # Update existing tracker data
                self._known_devices[mac].update_data(dev_data)
            else:
                # Create new tracker for newly seen device
                tracker = MiWiFiDeviceTracker(
                    coordinator=self._coordinator,
                    mac=mac,
                    dev_data=dev_data,
                    model=self._model,
                    firmware=self._firmware,
                )
                self._known_devices[mac] = tracker
                new_entities.append(tracker)

        if new_entities:
            self._async_add_entities(new_entities, update_before_add=True)

        # Mark devices not in current poll as offline
        current_macs = set(devices.keys())
        for mac, tracker in self._known_devices.items():
            if mac not in current_macs:
                tracker.update_data({**tracker._dev_data, "online": 0})


class MiWiFiDeviceTracker(CoordinatorEntity[MiWiFiCoordinator], TrackerEntity):
    """Representation of a network device tracked by MiWiFi Router.

    Each device shows:
    - State: home / not_home (based on router's 'online' field)
    - Attributes: per-device upload/download speed, totals, signal, etc.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MiWiFiCoordinator,
        mac: str,
        dev_data: dict[str, Any],
        model: str,
        firmware: str,
    ) -> None:
        """Initialize the device tracker."""
        super().__init__(coordinator)
        self._mac = mac
        self._dev_data = dev_data
        self._model = model
        self._firmware = firmware

        # Unique ID based on router host + device MAC
        self._attr_unique_id = f"{coordinator.api._host}_device_{mac}"

        # Friendly name - prefer hostname from device_list, then devname
        name = dev_data.get("name", "")
        if not name or name == mac:
            # Try hostname field (from xqsystem/device_list)
            name = dev_data.get("hostname", dev_data.get("name", ""))
        if not name or name == mac:
            name = f"Device {mac}"
        self._attr_name = name

        # Initial connection state
        online_val = dev_data.get("online", 0)
        self._attr_is_connected = int(online_val) > 0 if online_val else False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update - data is managed by MiWiFiTrackerManager."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info - link all trackers to the router device."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.api._host)},
            "name": self._model or "MiWiFi Router",
            "manufacturer": "Xiaomi",
            "model": self._model,
            "sw_version": self._firmware,
        }

    @property
    def source_type(self) -> SourceType:
        """Return the source type of the device tracker."""
        return SourceType.ROUTER

    @property
    def ip_address(self) -> str | None:
        """Return the IP address of the device."""
        return self._dev_data.get("ip") or None

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac

    @property
    def is_connected(self) -> bool:
        """Return if the device is connected."""
        return self._attr_is_connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return per-device speed and detail attributes."""
        upspeed = int(self._dev_data.get("upspeed", 0))
        downspeed = int(self._dev_data.get("downspeed", 0))
        upload = int(self._dev_data.get("upload", 0))
        download = int(self._dev_data.get("download", 0))
        online_seconds = int(self._dev_data.get("online", 0))

        attrs: dict[str, Any] = {
            "mac": self._mac,
            "ip": self._dev_data.get("ip", ""),
            # Per-device speed (raw bytes/sec)
            "upload_speed": upspeed,
            "upload_speed_human": self._format_speed(upspeed),
            "download_speed": downspeed,
            "download_speed_human": self._format_speed(downspeed),
            # Per-device cumulative totals
            "upload_total": upload,
            "upload_total_human": self._format_bytes(upload),
            "download_total": download,
            "download_total_human": self._format_bytes(download),
            # Peak speeds
            "max_upload_speed": int(self._dev_data.get("maxuploadspeed", 0)),
            "max_download_speed": int(self._dev_data.get("maxdownloadspeed", 0)),
            # Online duration in seconds
            "online_seconds": online_seconds,
            "is_ap": bool(int(self._dev_data.get("isap", 0))),
        }

        # Optional fields from device_list endpoint
        if "signal" in self._dev_data and self._dev_data["signal"]:
            attrs["signal"] = int(self._dev_data["signal"])
        if "channel" in self._dev_data and self._dev_data["channel"]:
            attrs["channel"] = self._dev_data["channel"]
        if "oui" in self._dev_data and self._dev_data["oui"]:
            attrs["oui"] = self._dev_data["oui"]
        if "hostname" in self._dev_data and self._dev_data["hostname"]:
            attrs["hostname"] = self._dev_data["hostname"]

        return attrs

    @staticmethod
    def _format_speed(speed_bytes: int) -> str:
        """Format speed for human readability."""
        if speed_bytes >= 1_000_000:
            return f"{speed_bytes / 1_000_000:.2f} MB/s"
        if speed_bytes >= 1_000:
            return f"{speed_bytes / 1_000:.2f} KB/s"
        return f"{speed_bytes} B/s"

    @staticmethod
    def _format_bytes(total_bytes: int) -> str:
        """Format bytes for human readability."""
        if total_bytes >= 1_000_000_000_000:
            return f"{total_bytes / 1_000_000_000_000:.2f} TB"
        if total_bytes >= 1_000_000_000:
            return f"{total_bytes / 1_000_000_000:.2f} GB"
        if total_bytes >= 1_000_000:
            return f"{total_bytes / 1_000_000:.2f} MB"
        if total_bytes >= 1_000:
            return f"{total_bytes / 1_000:.2f} KB"
        return f"{total_bytes} B"

    def update_data(self, dev_data: dict[str, Any]) -> None:
        """Update device data and connection state."""
        self._dev_data = dev_data
        online_val = dev_data.get("online", 0)
        self._attr_is_connected = int(online_val) > 0 if online_val else False

        # Update name if we got a more descriptive one
        name = dev_data.get("name", "")
        hostname = dev_data.get("hostname", "")
        # Prefer hostname from device_list if available
        preferred_name = hostname if hostname and hostname != self._mac else name
        if preferred_name and preferred_name != self._mac and preferred_name.strip():
            self._attr_name = preferred_name

        self.async_write_ha_state()
