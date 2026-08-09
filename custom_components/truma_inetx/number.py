"""Number platform for the Truma iNet X fan level."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up the Truma fan-level number."""
    async_add_entities([TrumaFanLevel(entry.runtime_data)])


class TrumaFanLevel(TrumaEntity, NumberEntity):
    """Fan circulation level (0-10)."""

    _attr_translation_key = "fan_level"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "fan_level")

    @property
    def native_value(self) -> float | None:
        """Current fan level."""
        return self.data.fan_level

    async def async_set_native_value(self, value: float) -> None:
        """Set the fan level."""
        await self.coordinator.async_write("AirCirculation", "FanLevel", int(value))
