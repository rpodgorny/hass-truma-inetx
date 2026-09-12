"""Number platform for Truma fan level and manual live duration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import UnitOfTime
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
    """Set up Truma number controls."""
    async_add_entities(
        [TrumaFanLevel(entry.runtime_data), TrumaManualLiveMinutes(entry.runtime_data)]
    )


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


class TrumaManualLiveMinutes(TrumaEntity, RestoreNumber):
    """User-selected duration for an immediate manual BLE session."""

    _attr_translation_key = "manual_live_minutes"
    _attr_native_min_value = 0
    _attr_native_max_value = 999
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize with the safe one-refresh default."""
        super().__init__(coordinator, "manual_live_minutes")
        self._attr_native_value = 0

    async def async_added_to_hass(self) -> None:
        """Restore the user's last duration after a restart."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_number_data()
        if restored is not None:
            try:
                await self.async_set_native_value(restored.native_value)
            except (TypeError, ValueError):
                self._attr_native_value = 0
                self.coordinator.manual_live_minutes = 0
        else:
            self.coordinator.manual_live_minutes = 0

    async def async_set_native_value(self, value: float) -> None:
        """Store a whole number of minutes locally; do not touch BLE."""
        if isinstance(value, bool):
            raise ValueError("Live mode duration must be a whole number")
        numeric = float(value)
        if not numeric.is_integer() or not 0 <= numeric <= 999:
            raise ValueError(
                "Live mode duration must be a whole number from 0 to 999 minutes"
            )
        minutes = int(numeric)
        self._attr_native_value = minutes
        self.coordinator.manual_live_minutes = minutes
        self.async_write_ha_state()
