"""Switch platform for the Truma iNet X diesel burner."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
    """Set up the Truma diesel switch."""
    async_add_entities([TrumaDieselSwitch(entry.runtime_data)])


class TrumaDieselSwitch(TrumaEntity, SwitchEntity):
    """Diesel burner on/off."""

    _attr_name = "Diesel burner"
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:fuel"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "diesel")

    @property
    def is_on(self) -> bool | None:
        """Whether the diesel burner is enabled."""
        if self.data.diesel_level is None:
            return None
        return bool(self.data.diesel_level)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the diesel burner."""
        await self.coordinator.async_write("EnergySrc", "DieselLevel", 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the diesel burner."""
        await self.coordinator.async_write("EnergySrc", "DieselLevel", 0)
