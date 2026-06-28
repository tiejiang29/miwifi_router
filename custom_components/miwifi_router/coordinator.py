"""DataUpdateCoordinator with layered polling strategy for MiWiFi Router.

Polling tiers:
- Tier 1 (realtime): WAN speeds, device counts, per-device speeds — 10s
- Tier 2 (devices): Full device list with details — 30s
- Tier 3 (static): Hardware/firmware info — 5 min (cached in API client)

Re-authorization strategy (inspired by hass-miwifi):
- _is_reauthorization flag: when any API call fails with auth error,
  set this flag so the next poll cycle will re-login first before
  fetching any data.
- _is_first_update flag: on the very first update, if it fails,
  retry with exponential backoff (up to MAX_RETRIES times) before
  giving up. This handles transient issues during startup.
- Grace period: when a non-first update gets an auth error, we return
  the previous data instead of raising UpdateFailed. This keeps
  entities available for one more cycle. Only if re-auth also fails
  do we raise UpdateFailed and entities become unavailable.
  CoordinatorEntity.available uses coordinator.last_update_success,
  which stays True as long as we return data (not raise).
"""

from __future__ import annotations

import asyncio
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

MAX_FIRST_UPDATE_RETRIES = 5


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
        """Merge device data from device_list and status endpoints.

        device_list provides ALL connected devices with speeds (from
        statistics sub-object), while status only returns top-N devices
        by traffic (rest are lumped into "Others"). So we prefer
        device_list for device speeds, and use status for WAN/CPU/mem.

        Merge priority:
        1. Start with device_list data (complete device list + speeds)
        2. Enrich with status data for devices that appear in both
           (status may have slightly more real-time speeds)
        3. Status-only devices (like "Others") are skipped — they are
           aggregates, not real devices
        """
        merged: dict[str, dict[str, Any]] = {}

        # First pass: data from device_list endpoint (most complete)
        for dev in self.device_list.get("dev", []):
            mac = dev.get("mac", "")
            if not mac:
                continue
            merged[mac] = {**dev}

        # Second pass: enrich with status data
        for dev in self.status.get("dev", []):
            mac = dev.get("mac", "")
            if not mac:
                continue
            # Skip "Others" aggregate entry (empty MAC or name="Others")
            if dev.get("devname", "") == "Others":
                continue
            if mac in merged:
                # If status has non-zero speed and device_list has zero,
                # use status speed (status updates more frequently)
                status_up = int(dev.get("upspeed", 0))
                status_down = int(dev.get("downspeed", 0))
                if status_up > 0 and int(merged[mac].get("upspeed", 0)) == 0:
                    merged[mac]["upspeed"] = status_up
                if status_down > 0 and int(merged[mac].get("downspeed", 0)) == 0:
                    merged[mac]["downspeed"] = status_down
                # Fill in any missing fields from status
                for key, value in dev.items():
                    if key not in merged[mac] or not merged[mac][key]:
                        merged[mac][key] = value
            else:
                # Device only in status (not in device_list) — add it
                merged[mac] = {**dev}

        self.devices = merged
        return merged


class MiWiFiCoordinator(DataUpdateCoordinator):
    """Coordinator with layered polling and re-authorization support.

    Inspired by hass-miwifi's LuciUpdater pattern:
    - _is_reauthorization: set True when auth fails, triggers re-login next cycle
    - _is_first_update: enables retry with backoff on first update
    - Grace period: return previous data on first auth failure instead of
      raising UpdateFailed — this keeps CoordinatorEntity.available = True
    """

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
        # Re-authorization flag — starts True so first update will login
        self._is_reauthorization: bool = True
        # First update flag — enables retry with backoff
        self._is_first_update: bool = True
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
        """Fetch data from the router using layered polling strategy.

        Re-authorization flow:
        1. If _is_reauthorization is True, login first before fetching data
        2. If data fetch fails with auth error, set _is_reauthorization = True
           for the next cycle
        3. On first update, retry with exponential backoff on failure
        4. Grace period: on non-first update, return previous data instead
           of raising UpdateFailed — this keeps entities available
        """
        return await self._update_with_retry()

    async def _update_with_retry(self, retry: int = 0) -> MiWiFiRouterData:
        """Internal update method with retry support for first update."""
        now = time.time()
        _was_reauthorization = self._is_reauthorization

        try:
            # ---- Login if needed ----
            # If re-authorization is flagged, ensure we have a fresh stok
            if self._is_reauthorization:
                _LOGGER.debug(
                    "Re-authorization flagged, ensuring fresh stok for %s",
                    self._api._host,
                )
                # Force re-login by invalidating current stok
                self._api.invalidate_stok()
                # The next _api_get() call will trigger a fresh login

            # ---- Tier 1: Always poll realtime data ----
            self._data.status = await self._api.get_status()

            # ---- Tier 2: Poll device list at lower frequency ----
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
            if (now - self._last_init_poll) >= 300:
                try:
                    self._data.init_info = await self._api.get_init_info()
                    self._last_init_poll = now
                except (MiWiFiConnectionError, MiWiFiAuthError) as err:
                    _LOGGER.debug("Init info poll failed: %s", err)

            # ---- Merge device data ----
            self._data.get_merged_devices()

            # Success! Clear flags
            self._is_reauthorization = False
            self._auth_failures = 0

            if self._is_first_update:
                _LOGGER.info(
                    "First update successful for %s",
                    self._api._host,
                )
                self._is_first_update = False

            return self._data

        except MiWiFiAuthError as err:
            self._auth_failures += 1
            # Mark for re-authorization on next cycle
            self._is_reauthorization = True

            # Grace period: on non-first update, if we weren't already
            # in re-authorization before this cycle, return previous data
            # instead of raising UpdateFailed. This keeps
            # coordinator.last_update_success = True, so entities stay
            # available for one more cycle. Next cycle will re-login.
            if not self._is_first_update and not _was_reauthorization:
                _LOGGER.warning(
                    "Auth error during poll for %s (grace period, returning previous data): %s",
                    self._api._host, err,
                )
                return self._data

            # First update: retry with exponential backoff
            if self._is_first_update and retry < MAX_FIRST_UPDATE_RETRIES:
                backoff = (retry + 1) * 2  # 2s, 4s, 6s, 8s, 10s
                _LOGGER.warning(
                    "First update auth error for %s (retry %d/%d in %ds): %s",
                    self._api._host,
                    retry + 1, MAX_FIRST_UPDATE_RETRIES, backoff, err,
                )
                await asyncio.sleep(backoff)
                return await self._update_with_retry(retry + 1)

            # Exhausted retries or repeated auth failure
            if self._auth_failures >= 3:
                _LOGGER.error(
                    "Authentication failed %d times in a row for %s. "
                    "Please check your password and reconfigure the integration.",
                    self._auth_failures, self._api._host,
                )
            raise UpdateFailed(f"Authentication error: {err}") from err

        except MiWiFiConnectionError as err:
            # Connection errors don't trigger re-authorization
            self._is_reauthorization = False

            # First update: retry with exponential backoff
            if self._is_first_update and retry < MAX_FIRST_UPDATE_RETRIES:
                backoff = (retry + 1) * 2
                _LOGGER.warning(
                    "First update connection error for %s (retry %d/%d in %ds): %s",
                    self._api._host,
                    retry + 1, MAX_FIRST_UPDATE_RETRIES, backoff, err,
                )
                await asyncio.sleep(backoff)
                return await self._update_with_retry(retry + 1)

            raise UpdateFailed(f"Connection error: {err}") from err

        except Exception as err:
            # First update: retry with exponential backoff
            if self._is_first_update and retry < MAX_FIRST_UPDATE_RETRIES:
                backoff = (retry + 1) * 2
                _LOGGER.warning(
                    "First update unexpected error for %s (retry %d/%d in %ds): %s",
                    self._api._host,
                    retry + 1, MAX_FIRST_UPDATE_RETRIES, backoff, err,
                )
                await asyncio.sleep(backoff)
                return await self._update_with_retry(retry + 1)

            raise UpdateFailed(f"Unexpected error: {err}") from err
