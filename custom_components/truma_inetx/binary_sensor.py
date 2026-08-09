"""Binary sensor platform for Truma iNet X flame and link status."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [TrumaFlameSensor(coordinator), TrumaConnectionSensor(coordinator)]
    )


class TrumaFlameSensor(TrumaEntity, BinarySensorEntity):
    """Flame/burner running status."""

    _attr_translation_key = "flame"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "flame")

    @property
    def is_on(self) -> bool | None:
        """Whether the burner flame is active."""
        if self.data.flame_status is None:
            return None
        return bool(self.data.flame_status)


class TrumaConnectionSensor(TrumaEntity, BinarySensorEntity):
    """BLE link status to the panel."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = None
    _gate_on_connected = False  # reports the link itself → never gated

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "connection")

    @property
    def is_on(self) -> bool:
        """Whether the BLE link to the panel is up."""
        return self.data.connected
