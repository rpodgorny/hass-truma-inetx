"""Sensor platform for Truma iNet X temperatures and voltage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry
from .entity import TrumaEntity
from .truma.state import TrumaState

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TrumaSensorDescription(SensorEntityDescription):
    """Describes a Truma sensor."""

    value_fn: Callable[[TrumaState], float | None]


SENSORS: tuple[TrumaSensorDescription, ...] = (
    TrumaSensorDescription(
        key="current_temp",
        name="Room temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.air_current_temp),
    ),
    TrumaSensorDescription(
        key="water_temp",
        name="Water temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.water_current_temp),
    ),
    TrumaSensorDescription(
        key="internal_temp",
        name="Internal temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.internal_temp),
    ),
    TrumaSensorDescription(
        key="voltage",
        name="Supply voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        # The panel reports millivolts, so we can show 2 decimals. Set the
        # display precision explicitly: the VOLTAGE device class otherwise
        # defaults to whole volts and hides the decimals we actually have.
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda s: (
            None if s.voltage_vcc12 is None else round(s.voltage_vcc12 / 1000.0, 2)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma sensors."""
    coordinator = entry.runtime_data
    async_add_entities(TrumaSensor(coordinator, desc) for desc in SENSORS)


class TrumaSensor(TrumaEntity, SensorEntity):
    """A Truma numeric sensor."""

    entity_description: TrumaSensorDescription

    def __init__(self, coordinator, description: TrumaSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.data)
