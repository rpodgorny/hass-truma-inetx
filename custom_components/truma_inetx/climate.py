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

from .coordinator import TrumaConfigEntry
from .entity import TrumaEntity
from .truma.state import TrumaState

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

# AirCirculation.FanLevel is 0-10. Exposing it as the climate entity's fan mode
# puts it in the same card as the mode and setpoint, which is where you want it
# in FAN_ONLY. The dedicated "Fan level" number entity still exists for
# automations. Both lists are derived from one dict so they cannot drift.
_LEVEL_TO_FAN_MODE = {0: FAN_OFF} | {level: str(level) for level in range(1, 11)}
_FAN_MODE_TO_LEVEL = {v: k for k, v in _LEVEL_TO_FAN_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Truma climate entity."""
    async_add_entities([TrumaClimate(entry.runtime_data)])


class TrumaClimate(TrumaEntity, ClimateEntity):
    """Room heating as an HA climate entity."""

    _attr_name = None  # primary feature → uses the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_fan_modes = list(_LEVEL_TO_FAN_MODE.values())
    _attr_min_temp = 5
    _attr_max_temp = 30
    _attr_target_temperature_step = 1

    def __init__(self, coordinator) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, "room_climate")

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
        values = self.data.allowed_values("RoomClimate", "Mode")
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
    def current_temperature(self) -> float | None:
        """Current room temperature (from the air-heating sensor)."""
        return TrumaState.wire_to_celsius(self.data.air_current_temp)

    @property
    def target_temperature(self) -> float | None:
        """Target room temperature."""
        # Live setpoint lives on the heater (AirHeating), not the panel mirror
        # (RoomClimate.TgtTemp only echoes our own writes). Matches current_temp.
        return TrumaState.wire_to_celsius(self.data.air_target_temp)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current heating mode."""
        if self.data.room_mode is None:
            return None
        return _MODE_TO_HVAC.get(self.data.room_mode, HVACMode.OFF)

    @property
    def fan_mode(self) -> str | None:
        """Current circulation fan level, as a fan mode."""
        if self.data.fan_level is None:
            return None
        # An unexpected level is reported as unknown rather than a value the
        # frontend would reject for not being in fan_modes.
        return _LEVEL_TO_FAN_MODE.get(self.data.fan_level)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the circulation fan level."""
        await self.coordinator.async_write(
            "AirCirculation", "FanLevel", _FAN_MODE_TO_LEVEL[fan_mode]
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the heating mode."""
        await self.coordinator.async_write(
            "RoomClimate", "Mode", _HVAC_TO_MODE[hvac_mode]
        )

    async def async_turn_on(self) -> None:
        """Turn heating on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn heating off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target room temperature."""
        temperature = kwargs[ATTR_TEMPERATURE]
        await self.coordinator.async_write(
            "AirHeating", "TgtTemp", int(round(temperature * 10))
        )
