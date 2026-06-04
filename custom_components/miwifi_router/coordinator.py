"""DataUpdateCoordinator with layered polling strategy for MiWiFi Router.

Polling tiers:
- Tier 1 (realtime): WAN speeds, device counts, per-device speeds — 10s
- Tier 2 (devices): Full device list with details — 30s
- Tier 3 (static): Hardware/firmware info — 5 min (cached in API client)
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import MiWiFiAPIClient, MiWiFiAuthError, MiWiFiConnectionError
from .const import DEFAULT_DEVICE_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class MiWiFiRouterData:
    """Container for all router data, separated by polling tier."""

    def __init__(self) -> None:
        # Tier 1: Realtime data (high-frequency poll)
        self.status: dict[str, Any] = {}
        # Tier 2: Device list details (medium-frequency poll)
        self.device_list: dict[str, Any] = {}
        # Tier 3: Static data (low-frequency poll)
        self.init_info: dict[str, Any] = {}
        # Merged device data from all sources
        self.devices: dict[str, dict[str, Any]] = {}
        # Previous online count for change detection
        self._prev_online_count: int = -1

    def get_online_count(self) -> int:
        """Get current online device count."""
        return self.status.get("count", {}).get("online", 0)

    def get_all_count(self) -> int:
        """Get total device count."""
        return self.status.get("count", {}).get("all", 0)

    def has_online_count_changed(self) -> bool:
        """Check if online device count changed since last check."""
        current = self.get_online_count()
        changed = current != self._prev_online_count
        self._prev_online_count = current
        return changed

    def get_merged_devices(self) -> dict[str, dict[str, Any]]:
        """Merge device data from status and device_list endpoints.

        Status provides per-device speeds (upspeed/downspeed).
        Device_list provides more detail (signal, channel, oui, hostname, etc).
        We merge by MAC address, preferring the most descriptive name.
        """
        merged: dict[str, dict[str, Any]] = {}

        # First pass: data from status endpoint (has speeds)
        for dev in self.status.get("dev", []):
            mac = dev.get("mac", "")
            if not mac:
                continue
            merged[mac] = {**dev}

        # Second pass: enrich with device_list data
        for dev in self.device_list.get("dev", []):
            mac = dev.get("mac", "")
            if not mac:
                continue
            if mac in merged:
                # Merge extra fields from device_list
                for key, value in dev.items():
                    if key not in merged[mac] or not merged[mac][key]:
                        merged[mac][key] = value
                    # Prefer device_list name if it's more descriptive
                    if key == "name" and value and value != mac:
                        merged[mac][key] = value
            else:
                # Device only in device_list (might have gone offline)
                merged[mac] = {**dev}

        self.devices = merged
        return merged


class MiWiFiCoordinator(DataUpdateCoordinator):
    """Coordinator with layered polling: different intervals for different data tiers."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MiWiFiAPIClient,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        device_scan_interval: int = DEFAULT_DEVICE_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_coordinator",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._api = api
        self._scan_interval = scan_interval
        self._device_scan_interval = device_scan_interval
        self._data = MiWiFiRouterData()
        # Track device list poll timing
        self._last_device_poll: float = 0
        self._last_init_poll: float = 0
        # Auth failure counter for graceful degradation
        self._auth_failures: int = 0

    @property
    def api(self) -> MiWiFiAPIClient:
        """Return the API client."""
        return self._api

    @property
    def router_data(self) -> MiWiFiRouterData:
        """Return the router data container."""
        return self._data

    async def _async_update_data(self) -> MiWiFiRouterData:
        """Fetch data from the router using layered polling strategy."""
        now = time.time()

        try:
            # ---- Tier 1: Always poll realtime data ----
            self._data.status = await self._api.get_status()

            # ---- Tier 2: Poll device list at lower frequency ----
            # OR immediately if online count changed (smart trigger)
            count_changed = self._data.has_online_count_changed()
            device_poll_due = (
                now - self._last_device_poll
            ) >= self._device_scan_interval

            if device_poll_due or count_changed:
                if count_changed:
                    _LOGGER.debug(
                        "Online count changed, triggering immediate device list poll"
                    )
                try:
                    self._data.device_list = await self._api.get_device_list()
                    self._last_device_poll = now
                except (MiWiFiConnectionError, MiWiFiAuthError) as err:
                    _LOGGER.warning("Device list poll failed: %s", err)
                    # Don't fail the entire update - status data is still valid

            # ---- Tier 3: Poll init info at very low frequency ----
            # API client caches this for 5 minutes internally
            if (now - self._last_init_poll) >= 300:
                try:
                    self._data.init_info = await self._api.get_init_info()
                    self._last_init_poll = now
                except (MiWiFiConnectionError, MiWiFiAuthError) as err:
                    _LOGGER.debug("Init info poll failed: %s", err)

            # ---- Merge device data ----
            self._data.get_merged_devices()

            # Reset auth failure counter on success
            self._auth_failures = 0

            return self._data

        except MiWiFiAuthError as err:
            self._auth_failures += 1
            if self._auth_failures >= 3:
                _LOGGER.error(
                    "Authentication failed %d times in a row. "
                    "Please check your password and reconfigure the integration.",
                    self._auth_failures,
                )
            raise UpdateFailed(f"Authentication error: {err}") from err

        except MiWiFiConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
