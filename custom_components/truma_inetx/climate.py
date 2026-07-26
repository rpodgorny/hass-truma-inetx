"""Climate platform for Truma iNet X room heating."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry
from .entity import TrumaEntity
from .truma.state import RoomClimateMode, TrumaState

_HVAC_TO_MODE = {
    HVACMode.OFF: int(RoomClimateMode.OFF),
    HVACMode.HEAT: int(RoomClimateMode.HEATING),
    HVACMode.FAN_ONLY: int(RoomClimateMode.VENTILATING),
}
_MODE_TO_HVAC = {v: k for k, v in _HVAC_TO_MODE.items()}


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
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = 5
    _attr_max_temp = 30
    _attr_target_temperature_step = 1

    def __init__(self, coordinator) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, "room_climate")

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
