"""Climate platform for Truma iNet X room heating."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    FAN_OFF,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bus import Bus
from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, async_add_per_device

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0

# What a RoomClimate.Mode value means. 0, 3 and 5 are measured on two
# vehicles; 1, 2, 4 and 6 come from a Combi 6 E's panel (#11, #7) and from an
# independent decoding of the protocol, and cost nothing to name here -- a
# vehicle whose panel does not enumerate them never reaches them.
#
# 4 is heating with air-conditioner assistance, which is still heating as far
# as Home Assistant's model goes; it shares HVACMode.HEAT and the reverse
# table below deliberately sends plain heating instead.
_MODE_TO_HVAC = {
    0: HVACMode.OFF,
    1: HVACMode.AUTO,
    2: HVACMode.COOL,
    3: HVACMode.HEAT,
    4: HVACMode.HEAT,
    5: HVACMode.FAN_ONLY,
    6: HVACMode.DRY,
}
_HVAC_TO_MODE = {
    HVACMode.OFF: 0,
    HVACMode.AUTO: 1,
    HVACMode.COOL: 2,
    HVACMode.HEAT: 3,
    HVACMode.FAN_ONLY: 5,
    HVACMode.DRY: 6,
}
# What to offer while the panel has described nothing: the three modes every
# vehicle seen so far has, which is what this entity offered unconditionally
# before the panel was asked.
_DEFAULT_HVAC_MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]

# The fan level exposed as the climate entity's fan mode, which puts it in the
# same card as the mode and setpoint -- where you want it in FAN_ONLY. The
# dedicated "Fan level" number entity still exists for automations. The range
# comes from the appliance itself (see fan_modes); 0-10 is only the fallback
# for one that describes none, and used to be handed to every device that
# published the parameter, roof air conditioners included.
_FALLBACK_FAN_LEVELS = (0, 10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a climate entity per appliance that heats the air.

    ``AirHeating.Temp`` is the defining parameter: an appliance that reports
    the air temperature it is working against is the one this entity controls.
    On every vehicle seen so far that is the Combi, but naming the heater's
    address here would be the same mistake that sent cooling commands to it
    (#10) -- addresses are renumbered when a device is re-paired.
    """
    coordinator = entry.runtime_data
    async_add_per_device(
        coordinator,
        async_add_entities,
        "AirHeating",
        "Temp",
        lambda addr: TrumaClimate(coordinator, addr),
    )


class TrumaClimate(TrumaEntity, ClimateEntity):
    """Room heating as an HA climate entity.

    The only entity here that is not one bus parameter: a mode, a setpoint, a
    reading and a fan speed at once, and they do not all come from the same
    device. The setpoint, the reading and the fan are this appliance's own.
    The mode is ``RoomClimate``, which belongs to the panel -- it is the panel
    relaying the room's mode to whichever appliance serves the room, so the
    heater does not publish it and a write to it goes back to the panel.
    """

    _attr_name = None  # primary feature → uses the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5
    _attr_max_temp = 30
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: TrumaCoordinator, addr: int) -> None:
        """Initialize the climate entity for one appliance."""
        super().__init__(coordinator, addr, "room_climate")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Offer only the control the current mode actually uses.

        The panel is not a conventional thermostat. Heating is thermostatic and
        the panel picks the fan speed itself, so the setpoint is the only useful
        control. Ventilating has no setpoint at all, only a fan level. Offering
        both at once invites setting the one the panel is ignoring, so each mode
        exposes just its own control and the card follows the mode.

        Home Assistant reads this per state write and rebuilds the entity's
        capability attributes from it, so the frontend switches with the mode.
        """
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self.hvac_mode is HVACMode.FAN_ONLY:
            return features | ClimateEntityFeature.FAN_MODE
        # HEAT, OFF, and the not-yet-known case all keep the setpoint: while off
        # it is the resting target you come back to, which is how every other
        # thermostat in HA behaves.
        return features | ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """The modes this vehicle actually has, as its panel enumerates them.

        A fixed list is wrong in both directions: it offers cooling on a van
        with no air conditioner, and it withholds it from one that has (#11).
        The panel enumerates the parameter per installation, so that is what is
        offered -- names ignored, since they arrive in the panel's language;
        only which values exist crosses over.

        A value the panel offers and the table above has no name for is left
        out rather than guessed at, and OFF is always offered: a heating
        control that cannot be switched off is worse than one that shows a mode
        the panel did not mention.
        """
        panel = self.bus.sole_publisher("RoomClimate", "Mode")
        values = None if panel is None else panel.allowed_values("RoomClimate", "Mode")
        if values is None:
            return _DEFAULT_HVAC_MODES
        modes: list[HVACMode] = []
        for value in values:
            mode = _MODE_TO_HVAC.get(value)
            if mode is not None and mode not in modes:
                modes.append(mode)
        if not modes:
            return _DEFAULT_HVAC_MODES
        if HVACMode.OFF not in modes:
            modes.insert(0, HVACMode.OFF)
        return modes

    @property
    def fan_modes(self) -> list[str]:
        """The circulation levels this appliance describes, as fan modes."""
        low, high = (
            self.device.bounds("AirCirculation", "FanLevel") or _FALLBACK_FAN_LEVELS
        )
        return [self._fan_label(level) for level in range(int(low), int(high) + 1)]

    @staticmethod
    def _fan_label(level: int) -> str:
        """The fan mode string for a circulation level."""
        return FAN_OFF if level == 0 else str(level)

    @property
    def current_temperature(self) -> float | None:
        """Current room temperature, as this appliance measures it."""
        return Bus.wire_to_celsius(self.device.get("AirHeating", "Temp"))

    @property
    def target_temperature(self) -> float | None:
        """Target room temperature."""
        # The live setpoint lives on the appliance (AirHeating), not on the
        # panel mirror -- RoomClimate.TgtTemp only echoes our own writes. Same
        # source as current_temperature, so the two cannot disagree.
        return Bus.wire_to_celsius(self.device.get("AirHeating", "TgtTemp"))

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current heating mode, as the panel relays it."""
        mode = self.bus.relayed("RoomClimate", "Mode")
        if not isinstance(mode, int):
            return None
        return _MODE_TO_HVAC.get(mode, HVACMode.OFF)

    @property
    def fan_mode(self) -> str | None:
        """This appliance's own circulation level, as a fan mode.

        Its own, not the bus's: a Combi and a roof air conditioner both
        publish AirCirculation.FanLevel, and reading them flat showed the
        Combi running at 4 as a 2 because the roof unit spoke last (#9).
        """
        level = self.device.get("AirCirculation", "FanLevel")
        if not isinstance(level, int):
            return None
        label = self._fan_label(level)
        # A level outside what the appliance described is reported as unknown
        # rather than as a value the frontend would reject for not being in
        # fan_modes.
        return label if label in self.fan_modes else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set this appliance's circulation level."""
        level = 0 if fan_mode == FAN_OFF else int(fan_mode)
        await self.coordinator.async_write(
            self._addr, "AirCirculation", "FanLevel", level
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the heating mode (relayed by the panel)."""
        await self.coordinator.async_write(
            self._addr, "RoomClimate", "Mode", _HVAC_TO_MODE[hvac_mode]
        )

    async def async_turn_on(self) -> None:
        """Turn heating on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn heating off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target room temperature on this appliance."""
        temperature = kwargs[ATTR_TEMPERATURE]
        await self.coordinator.async_write(
            self._addr, "AirHeating", "TgtTemp", int(round(temperature * 10))
        )
