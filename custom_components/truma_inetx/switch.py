"""Switch platform for the Truma iNet X diesel burner, water pump and water boost."""

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
    #
    # The same holds, and matters more, for the two water-priority switches
    # below: neither has been seen on a vehicle yet -- they come from the
    # reverse-engineered schema, not from a measurement -- so gating them is
    # what keeps a heater that never mentions them from being given a control
    # that writes into nothing.
    async_add_when_reported(
        coordinator,
        async_add_entities,
        {
            "EnergySrc.DieselLevel": lambda: TrumaDieselSwitch(coordinator),
            "Switches.FreshWaterPump": lambda: TrumaWaterPumpSwitch(coordinator),
            "WaterHeating.BoostMode": lambda: TrumaWaterBoostSwitch(coordinator),
            "WaterHeating.FasterHeatingMode": (
                lambda: TrumaFasterWaterHeatingSwitch(coordinator)
            ),
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


class TrumaWaterBoostSwitch(TrumaEntity, SwitchEntity):
    """Water boost on/off.

    ``WaterHeating.BoostMode``, which the panel offers as the mode that puts
    the burner's whole output into the boiler and stops heating the air while
    it does. Documented in the reverse-engineered schema in
    `daaaaan/truma-inetx-ble` as 0 = off, 1 = on, and nothing beyond that: what
    the heater does to the air heating meanwhile is the panel's business, and
    is not reported here.

    Untested against hardware -- no dump showing the parameter exists yet.
    Which is exactly why it waits to be reported before it is created: on a
    heater that never mentions it, this switch never appears.
    """

    _attr_translation_key = "water_boost"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "water_boost")

    @property
    def is_on(self) -> bool | None:
        """Whether water boost is on."""
        if self.data.water_boost is None:
            return None
        return bool(self.data.water_boost)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn water boost on."""
        await self.coordinator.async_write("WaterHeating", "BoostMode", 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn water boost off."""
        await self.coordinator.async_write("WaterHeating", "BoostMode", 0)


class TrumaFasterWaterHeatingSwitch(TrumaEntity, SwitchEntity):
    """Faster water heating on/off.

    ``WaterHeating.FasterHeatingMode``, the schema's second way of favouring
    the water, distinguished from boost above by the
    ``FasterHeatingModeTime`` that sits beside it -- a duration in seconds,
    exposed as its own sensor. Whether the panel's own "boost" button writes
    this one or ``BoostMode`` is unmeasured; both are offered so that the
    vehicle can answer it, and each appears only where its parameter is
    reported.
    """

    _attr_translation_key = "faster_water_heating"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "faster_water_heating")

    @property
    def is_on(self) -> bool | None:
        """Whether faster water heating is on."""
        if self.data.water_faster_heating is None:
            return None
        return bool(self.data.water_faster_heating)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn faster water heating on."""
        await self.coordinator.async_write("WaterHeating", "FasterHeatingMode", 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn faster water heating off."""
        await self.coordinator.async_write("WaterHeating", "FasterHeatingMode", 0)
