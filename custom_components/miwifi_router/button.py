"""Button platform for MiWiFi Router.

Provides a reboot button that sends the reboot command to the router
via the authenticated API. Uses the existing stok management mechanism.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Set up MiWiFi Router buttons from a config entry."""
    coordinator: MiWiFiCoordinator = hass.data[DOMAIN][entry.entry_id]
    api = coordinator.api

    entities = [
        MiWiFiRebootButton(
            coordinator=coordinator,
            description=ButtonEntityDescription(
                key="reboot",
                translation_key="reboot",
                icon="mdi:restart",
            ),
            model=api.model,
            firmware=api.firmware,
        ),
    ]

    async_add_entities(entities)


class MiWiFiRebootButton(CoordinatorEntity[MiWiFiCoordinator], ButtonEntity):
    """Button to reboot the router."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MiWiFiCoordinator,
        description: ButtonEntityDescription,
        model: str,
        firmware: str,
    ) -> None:
        """Initialize the reboot button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._model = model
        self._firmware = firmware
        self._attr_unique_id = f"{coordinator.api._host}_reboot"

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

    async def async_press(self) -> None:
        """Handle the button press — send reboot command to router.

        Uses the existing stok management mechanism:
        - _ensure_stok() validates current stok, re-login if expired
        - If stok is rejected (401/403), automatically re-login and retry
        - After reboot command is accepted, stok is invalidated
        - The next coordinator poll will detect the expired stok and
          re-login once the router comes back online
        """
        _LOGGER.info("Reboot button pressed for %s", self.coordinator.api._host)
        await self.coordinator.api.reboot()
