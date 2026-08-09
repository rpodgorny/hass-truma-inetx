"""Select platform for Truma iNet X water and electric heating modes."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0

WATER_OFF = "off"
WATER_OPTIONS = {WATER_OFF: None, "40 °C": 0, "60 °C": 1, "70 °C": 2}
_WATER_MODE_TO_LABEL = {0: "40 °C", 1: "60 °C", 2: "70 °C"}

ELECTRIC_OPTIONS = {"off": 0, "900 W": 1, "1800 W": 2}
_ELECTRIC_VALUE_TO_LABEL = {0: "off", 1: "900 W", 2: "1800 W"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma select entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        [TrumaWaterModeSelect(coordinator), TrumaElectricLevelSelect(coordinator)]
    )


class TrumaWaterModeSelect(TrumaEntity, SelectEntity):
    """Water heating mode (off / 40 / 60 / 70 °C)."""

    _attr_translation_key = "water_mode"
    _attr_options = list(WATER_OPTIONS)

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "water_mode")

    @property
    def current_option(self) -> str | None:
        """Return the current water heating mode."""
        if self.data.water_active == 0:
            return WATER_OFF
        if self.data.water_mode is None:
            return None
        return _WATER_MODE_TO_LABEL.get(self.data.water_mode)

    async def async_select_option(self, option: str) -> None:
        """Set the water heating mode."""
        if option == WATER_OFF:
            await self.coordinator.async_write("WaterHeating", "Active", 0)
            return
        await self.coordinator.async_write("WaterHeating", "Active", 1)
        await self.coordinator.async_write("WaterHeating", "Mode", WATER_OPTIONS[option])


class TrumaElectricLevelSelect(TrumaEntity, SelectEntity):
    """Supplemental electric heating level (off / 900 / 1800 W)."""

    _attr_translation_key = "electric_level"
    _attr_options = list(ELECTRIC_OPTIONS)

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "electric_level")

    @property
    def current_option(self) -> str | None:
        """Return the current electric heating level."""
        if self.data.electric_level is None:
            return None
        return _ELECTRIC_VALUE_TO_LABEL.get(self.data.electric_level)

    async def async_select_option(self, option: str) -> None:
        """Set the electric heating level."""
        await self.coordinator.async_write(
            "EnergySrc", "ElectricLevel", ELECTRIC_OPTIONS[option]
        )
