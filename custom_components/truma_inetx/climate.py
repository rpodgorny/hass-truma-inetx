"""Climate platform for Truma iNet X room heating."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    FAN_OFF,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bus import ActiveState, Bus
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
# A third panel, on a vehicle that does have a roof air conditioner,
# enumerates {0: Off, 1: ACC, 2: Cooling, 3: Heating, 5: Ventilating} (#23).
# It spells 1 "ACC" rather than automatic -- HVACMode.AUTO is still the
# closest thing HA has -- and 4 and 6 are absent even there, on the vehicle
# that would have been the one to show them. Both are still named from
# decoding alone.
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

# Which function each mode runs, and what the appliance is doing while that
# function reports ACTIVE. The topic is where the mode's own tri-state lives,
# the same way _setpoint resolves the mode's own setpoint field -- a mode is
# answered by the parameter it drives, not by one parameter for all of them.
#
# Deliberately not System.FlameStatus, which is the reading this entity looks
# like it should use and must not: that is the *appliance* making heat, water
# heating included, so it reads ACTIVE with the boiler working and the room
# untouched. Measured on a Combi 4 in dumps/combi4-inetx-pro/water-boost.json
# -- RoomClimate.Mode 0, AirHeating.Active 0, WaterHeating.Active 1,
# System.FlameStatus 1. Claiming the room was being heated there is #27 one
# level up: the right value, attached to the wrong thing (#30).
_MODE_FUNCTION = {
    HVACMode.HEAT: ("AirHeating", HVACAction.HEATING),
    HVACMode.COOL: ("AirCooling", HVACAction.COOLING),
    HVACMode.DRY: ("AirCooling", HVACAction.DRYING),
    HVACMode.FAN_ONLY: ("AirCirculation", HVACAction.FAN),
}
# In automatic the panel decides, and says so only by which function runs, so
# both are asked. Cooling first: a vehicle that reaches automatic at all has
# an air conditioner, and the two are not expected to run at once.
_AUTO_FUNCTIONS = (
    ("AirCooling", HVACAction.COOLING),
    ("AirHeating", HVACAction.HEATING),
)

# Degrees, per setpoint topic, for a device that describes no range of its
# own: the heater heats from 5 °C, and a room setpoint -- cooling, or the
# panel's own in automatic -- starts at 16, which is where the panel's slider
# starts and below which the bus refuses the write (bus.PARAM_VALIDATION).
# Only a fallback, and not the truth on any measured unit: the FreshJet 2200
# of #23 describes its own cooling setpoint as 16.0-31.0, and a device's own
# bounds always win (see _limit).
_FALLBACK_SETPOINT_RANGE = {
    "AirHeating": (5, 30),
    "AirCooling": (16, 30),
    "RoomClimate": (16, 30),
}

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

    def _setpoint(self) -> tuple[int, str]:
        """(address, topic) of the setpoint field the running mode moves.

        The panel shows one slider and keeps a separate field behind each
        mode. Measured on a Weinsberg with a roof air conditioner
        (2026-09-03): setting 17 °C while cooling put AirCooling.TgtTemp on
        the roof unit at 170 and left RoomClimate.TgtTemp sitting at 270,
        while the same slider in heating moved AirHeating.TgtTemp on the
        heater. Writing the heater's field while cooling -- which is what this
        entity used to do in every mode -- is acknowledged by the transport
        and changes nothing.

        Automatic is *not* measured. RoomClimate.TgtTemp is the assumption
        there: it is the field left over, and it belongs to the panel, which
        is what decides between heating and cooling in automatic.

        The heater's own field is the answer for heating, for off -- where the
        setpoint is the resting target you come back to -- and for any mode
        whose own field nothing on this bus publishes.
        """
        mode = self.hvac_mode
        if mode is HVACMode.COOL:
            cooler = self.bus.sole_publisher("AirCooling", "TgtTemp")
            if cooler is not None:
                return cooler.addr, "AirCooling"
        elif mode is HVACMode.AUTO:
            panel = self.bus.sole_publisher("RoomClimate", "TgtTemp")
            if panel is not None:
                return panel.addr, "RoomClimate"
        return self._addr, "AirHeating"

    @property
    def target_temperature(self) -> float | None:
        """Target temperature of the mode that is running."""
        addr, topic = self._setpoint()
        return Bus.wire_to_celsius(self.bus.device(addr).get(topic, "TgtTemp"))

    @property
    def min_temp(self) -> float:
        """The lowest the running mode's own field takes.

        Heating reaches down to 5 °C and a room setpoint does not -- a roof
        air conditioner stops at 16, and so does the panel's slider in
        cooling. The field's owner is asked first, the same way the fan range
        is; the two fallbacks are what the bus refuses below.
        """
        return self._limit(0)

    @property
    def max_temp(self) -> float:
        """The highest the running mode's own field takes."""
        return self._limit(1)

    def _limit(self, end: int) -> float:
        """One end of the running mode's setpoint range, in degrees."""
        addr, topic = self._setpoint()
        bounds = self.bus.device(addr).bounds(topic, "TgtTemp")
        if bounds is not None:
            return bounds[end] / 10
        return _FALLBACK_SETPOINT_RANGE[topic][end]

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current heating mode, as the panel relays it."""
        mode = self.bus.relayed("RoomClimate", "Mode")
        if not isinstance(mode, int):
            return None
        return _MODE_TO_HVAC.get(mode, HVACMode.OFF)

    def _function_state(self, topic: str) -> int | None:
        """The tri-state of one function, from the device that runs it.

        This appliance's own, except cooling: a Combi does not cool, and the
        roof unit that does is a device of its own -- the same resolution
        _setpoint makes for the cooling setpoint field.
        """
        if topic == "AirCooling":
            device = self.bus.sole_publisher(topic, "Active")
        else:
            device = self.device
        if device is None:
            return None
        value = device.get(topic, "Active")
        return value if isinstance(value, int) else None

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the appliance is doing now, as against what it was told to do.

        hvac_mode is the instruction; this is the answer. Without it a card
        cannot tell a heater that is firing from one standing by at target,
        and everything that wants both has to join this entity to the Heating
        and Cooling flags by hand -- on a vehicle where the cooling one lives
        on another device.

        Each mode is answered by its own function's tri-state (#30). ACTIVE
        means that function is working; IDLE is the appliance on with the room
        already where it was asked to be, which is the state the flags beside
        this entity exist to make visible, and OFF is reported only for the
        mode being off rather than for a function that reports nothing.

        Nothing here is a fourth reading of the bus: AirHeating.Active,
        AirCooling.Active and AirCirculation.Active are the same type-105
        family as System.FlameStatus, whose three states three appliances have
        measured (#15, #24, #23). AirHeating.Active has been seen at OFF and
        at IDLE on a Combi 4, where it tracked room heating alone while water
        heating ran on its own flag; ACTIVE is taken from the family rather
        than measured on that parameter.
        """
        mode = self.hvac_mode
        if mode is None:
            return None
        if mode is HVACMode.OFF:
            return HVACAction.OFF
        if mode is HVACMode.AUTO:
            for topic, action in _AUTO_FUNCTIONS:
                if self._function_state(topic) == ActiveState.ACTIVE:
                    return action
            return HVACAction.IDLE
        function = _MODE_FUNCTION.get(mode)
        if function is None:
            # A mode _MODE_TO_HVAC names and this table does not. None today;
            # if one is added there alone, unknown rather than a guess.
            return None
        topic, action = function
        state = self._function_state(topic)
        if state is None:
            # The mode is offered and its function says nothing -- unknown is
            # the honest answer, and this attribute is allowed to have none.
            return None
        return action if state == ActiveState.ACTIVE else HVACAction.IDLE

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
        """Set the target temperature in the field the running mode uses."""
        temperature = kwargs[ATTR_TEMPERATURE]
        addr, topic = self._setpoint()
        await self.coordinator.async_write(
            addr, topic, "TgtTemp", int(round(temperature * 10))
        )
