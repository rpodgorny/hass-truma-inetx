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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry
from .entity import TrumaEntity, async_add_when_reported
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
        translation_key="current_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.air_current_temp),
    ),
    TrumaSensorDescription(
        key="water_temp",
        translation_key="water_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.water_current_temp),
    ),
    TrumaSensorDescription(
        key="internal_temp",
        translation_key="internal_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
        value_fn=lambda s: TrumaState.wire_to_celsius(s.internal_temp),
    ),
    TrumaSensorDescription(
        key="voltage",
        translation_key="voltage",
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
    TrumaSensorDescription(
        key="flame_status",
        translation_key="flame_status",
        # No state class on purpose: 0, 1 and 2 are states, not a quantity, so
        # long-term statistics would average a code into a number that means
        # nothing.
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # The raw System.FlameStatus integer, for issue #15 and nothing else.
        #
        # The binary sensor beside it has to answer on/off, and does so by
        # treating anything non-zero as lit -- which is a guess, because the
        # parameter takes 0, 1 and 2 on a Combi 6 E and Truma publishes no
        # meaning for any of them. The panel does not name the values either:
        # it describes the parameter as type 105, the type it also gives
        # AirCirculation.Active, AirHeating.Active and WaterHeating.Active, and
        # those are the protocol's OFF / ACTIVE / IDLE triple. Strong evidence,
        # not proof, and only the raw number can settle it -- 1 while the
        # burner fires and 2 once the target is reached would confirm it.
        #
        # Disabled by default and filed as diagnostic: it exists so somebody
        # standing next to a running heater can watch the value in a history
        # graph, not because a user needs it.
        value_fn=lambda s: s.flame_status,
    ),
)

# Sensors for hardware most vehicles do not have. Each is created only once
# its parameter has actually been reported -- see async_add_when_reported.
# There is no water device class in Home Assistant, so these carry an icon
# instead (icons.json) and no device class at all.
OPTIONAL_SENSORS: tuple[TrumaSensorDescription, ...] = (
    TrumaSensorDescription(
        key="fresh_water_level",
        translation_key="fresh_water_level",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        # The tank sensor reports quarter steps (0/25/50/75/100 measured on a
        # Weinsberg), so a decimal place would invent precision.
        suggested_display_precision=0,
        value_fn=lambda s: s.fresh_water_level,
    ),
    TrumaSensorDescription(
        key="grey_water_level",
        translation_key="grey_water_level",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda s: s.grey_water_level,
    ),
    # The vehicle's batteries (#17). Reported by the electrical block, which
    # most installations do not have, so both wait for their parameter like
    # the tanks do. Tenths of a volt on the wire: 137 is 13.7 V.
    TrumaSensorDescription(
        key="starter_battery_voltage",
        translation_key="starter_battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        # One decimal is all the wire carries; the VOLTAGE device class would
        # otherwise round to whole volts and hide it.
        suggested_display_precision=1,
        value_fn=lambda s: (
            None
            if s.starter_battery_voltage is None
            else s.starter_battery_voltage / 10.0
        ),
    ),
    # The duration beside WaterHeating.FasterHeatingMode, in seconds. Whether
    # it counts down or states how long the mode was configured for is not
    # known -- the reverse-engineered schema says "duration in seconds" and
    # nothing else -- so it is reported raw and filed as diagnostic rather
    # than dressed up as a timer.
    TrumaSensorDescription(
        key="faster_water_heating_time",
        translation_key="faster_water_heating_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        # No state class: until it is known whether this counts down, a
        # long-term statistic of it would mean nothing.
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.water_faster_heating_time,
    ),
    TrumaSensorDescription(
        key="leisure_battery_voltage",
        translation_key="leisure_battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        value_fn=lambda s: (
            None
            if s.leisure_battery_voltage is None
            else s.leisure_battery_voltage / 10.0
        ),
    ),
)

# Which reported parameter proves the hardware behind each optional sensor.
OPTIONAL_SENSOR_PARAM = {
    "fresh_water_level": "FreshWater.Level",
    "grey_water_level": "GreyWater.Level",
    "starter_battery_voltage": "VBat.Voltage",
    "leisure_battery_voltage": "L1Bat.Voltage",
    "faster_water_heating_time": "WaterHeating.FasterHeatingModeTime",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma sensors."""
    coordinator = entry.runtime_data
    async_add_entities(TrumaSensor(coordinator, desc) for desc in SENSORS)
    async_add_when_reported(
        coordinator,
        async_add_entities,
        {
            OPTIONAL_SENSOR_PARAM[desc.key]: (
                lambda d=desc: TrumaSensor(coordinator, d)
            )
            for desc in OPTIONAL_SENSORS
        },
    )


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
