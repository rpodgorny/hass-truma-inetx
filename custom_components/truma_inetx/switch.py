"""Switch platform for the Truma iNet X diesel burner and water pump."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, async_add_when_reported

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Truma switches."""
    coordinator = entry.runtime_data
    # Neither switch is universal.
    #
    # A gas/electric Combi has no diesel burner, and its panel never mentions
    # EnergySrc.DieselLevel (#16) -- so the diesel switch was a control over
    # nothing there, exactly as the electric select was on a Combi D before it
    # started waiting for its own parameter. And only vehicles with a water
    # system have a pump to switch. In both cases the parameter arriving is
    # the evidence the hardware exists.
    async_add_when_reported(
        coordinator,
        async_add_entities,
        {
            "EnergySrc.DieselLevel": lambda: TrumaDieselSwitch(coordinator),
            "Switches.FreshWaterPump": lambda: TrumaWaterPumpSwitch(coordinator),
        },
    )


class TrumaDieselSwitch(TrumaEntity, SwitchEntity):
    """Diesel burner on/off."""

    _attr_translation_key = "diesel"
    _attr_device_class = SwitchDeviceClass.SWITCH

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


class TrumaWaterPumpSwitch(TrumaEntity, SwitchEntity):
    """Fresh-water pump on/off.

    The pump belongs to the vehicle's water hardware, not to the heater, so
    the write is addressed to whichever device reported ``Switches`` rather
    than to a device named here: that address differs per vehicle and changes
    when the device is re-paired. See ``TrumaState.get_command_dest``.
    """

    _attr_translation_key = "water_pump"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "water_pump")

    @property
    def is_on(self) -> bool | None:
        """Whether the fresh-water pump is running."""
        if self.data.water_pump is None:
            return None
        return bool(self.data.water_pump)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the pump."""
        await self.coordinator.async_write("Switches", "FreshWaterPump", 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the pump."""
        await self.coordinator.async_write("Switches", "FreshWaterPump", 0)
