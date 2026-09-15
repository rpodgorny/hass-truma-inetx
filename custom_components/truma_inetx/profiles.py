"""What a bus parameter looks like in Home Assistant.

One table, keyed by the parameter's own name on the wire::

    (topic, parameter) -> the rows it is presented as

A row is presentation and nothing else: which platform, which device class,
which unit, how to scale the wire value, which translation key names it. The
*device* it belongs to is not in here, because that is a property of the bus,
not of the parameter -- a Combi and a roof air conditioner both publish
``AirCirculation.FanLevel`` and each gets its own fan from this one row.

Three things follow from the table being a table:

* **Unmapped parameters are skipped.** Serial numbers, vendor blobs and the
  hundred-odd housekeeping parameters a panel publishes need no special case;
  they simply have no row.
* **New hardware is new rows.** It used to cost a typed state field, an entry
  in a topic map, a ``value_fn`` lambda and a row in an optional-sensor tuple,
  in four files.
* **The panel's own description refines a row, it never creates one.** 33
  topics with many parameters each would generate hundreds of nameless
  entities, and the panel's enum names arrive in its display language and
  differ per vehicle (#12), so they cannot be option strings either. What the
  description *is* good for is bounds, which values exist, and whether a
  parameter may be written at all -- see ``Device.bounds`` /
  ``allowed_values`` / ``writable``.

Qualifiers decorate a row rather than multiplying it. Two gas bottles are two
devices publishing one row, told apart by the device they hang off -- see
``TrumaCoordinator.device_info``, which names a device by its class instance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)

from .bus import ActiveState

# Wire scale for a temperature: the protocol carries tenths of a degree.
TENTHS = 0.1


@dataclass(frozen=True, kw_only=True)
class Row:
    """How one parameter is presented on one platform."""

    platform: Platform
    # Names the entity (strings.json) and its icon (icons.json), and is the
    # key half of the unique_id. Kept stable across presentation changes:
    # moving it orphans every entity built from this row.
    translation_key: str

    device_class: object | None = None
    state_class: object | None = None
    unit: str | None = None
    # Multiplier from the wire value to the native one. None means the wire
    # value is already what the unit says (a percentage, a level, a count).
    scale: float | None = None
    precision: int | None = None
    entity_category: EntityCategory | None = None
    enabled_default: bool = True

    # select: the label shown for each value. Ours, never the panel's -- its
    # names arrive in its display language, and an option string an automation
    # matches on must not change with the panel's language (#12). Which of
    # them to offer is still the panel's call.
    labels: dict[int, str] | None = None
    # select: the parameter in the same topic that switches the whole function
    # off, so the select can carry an "off" the panel also offers.
    off_param: str | None = None

    # binary_sensor: the values that read as on. None means "anything
    # non-zero", which is right for a plain flag and wrong for the tri-state
    # Active family.
    on_values: tuple[int, ...] | None = None

    # number: the step, and the range to use while the owning device has
    # described none of its own.
    step: float = 1
    fallback_bounds: tuple[int, int] | None = None

    # Turns a structured wire value into the single one the row presents,
    # applied before `scale`. Nearly every parameter carries a scalar and
    # leaves this None; a handful carry a list, and a sensor handed one
    # raises inside Home Assistant on every coordinator update rather than
    # once -- measured, with BleDeviceManagement.NrFreeSlots.
    reduce: Callable[[Any], Any] | None = None


def _free_slots(value: object) -> int | None:
    """Total free bond slots, out of the panel's own breakdown by device kind.

    Measured on the van, the parameter is not the count its name suggests::

        [{"type": 12, "nrOfSlots": 1}, {"type": 9, "nrOfSlots": 2}]

    -- a count per kind of device, and nothing the panel publishes says which
    kind is which, so the kinds are not named here and the entity is their
    sum: the number that answers whether the next bond will be refused. The
    list itself arrives in a diagnostics download unchanged, which is where
    the breakdown belongs until something explains the types.
    """
    if not isinstance(value, list):
        return None
    total = 0
    for entry in value:
        count = entry.get("nrOfSlots") if isinstance(entry, dict) else None
        if not isinstance(count, int):
            return None
        total += count
    return total


# Labels. Defined beside the rows that use them so a value and its name cannot
# drift apart.
_WATER_MODE_LABELS = {0: "Eco (40 °C)", 1: "Comfort (60 °C)", 2: "Hot (70 °C)"}
_ELECTRIC_LABELS = {0: "off", 1: "900 W", 2: "1800 W"}
_AIR_MODE_LABELS = {0: "Fast", 1: "Comfort"}

ROWS: dict[tuple[str, str], tuple[Row, ...]] = {
    # -- temperatures ----------------------------------------------------
    ("AirHeating", "Temp"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="current_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTemperature.CELSIUS,
            scale=TENTHS,
        ),
    ),
    ("WaterHeating", "Temp"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="water_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTemperature.CELSIUS,
            scale=TENTHS,
        ),
    ),
    ("Temperature", "Internal"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="internal_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTemperature.CELSIUS,
            scale=TENTHS,
            enabled_default=False,
        ),
    ),
    # -- electrics -------------------------------------------------------
    ("Eol", "Vcc12"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfElectricPotential.VOLT,
            # Millivolts here, unlike the vehicle batteries below. Two decimals
            # because the wire carries them; the VOLTAGE device class otherwise
            # rounds to whole volts and hides what we have.
            scale=0.001,
            precision=2,
            enabled_default=False,
        ),
    ),
    # The vehicle's batteries (#17), reported by the electrical block rather
    # than by the heater: VBat is the starter battery, L1Bat the leisure one.
    # Tenths of a volt on the wire -- 137 is 13.7 V -- which is a different
    # scale from Eol.Vcc12 above.
    ("VBat", "Voltage"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="starter_battery_voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfElectricPotential.VOLT,
            scale=TENTHS,
            precision=1,
        ),
    ),
    ("L1Bat", "Voltage"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="leisure_battery_voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfElectricPotential.VOLT,
            scale=TENTHS,
            precision=1,
        ),
    ),
    # -- water -----------------------------------------------------------
    # There is no water device class in Home Assistant, so these carry an icon
    # instead (icons.json) and no device class at all. The sensor reports
    # quarter steps (0/25/50/75/100, measured on a Weinsberg), so a decimal
    # place would invent precision.
    ("FreshWater", "Level"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="fresh_water_level",
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
            precision=0,
        ),
    ),
    ("GreyWater", "Level"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="grey_water_level",
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
            precision=0,
        ),
    ),
    ("Switches", "FreshWaterPump"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="water_pump",
            device_class=SwitchDeviceClass.SWITCH,
        ),
    ),
    ("WaterHeating", "Mode"): (
        Row(
            platform=Platform.SELECT,
            translation_key="water_mode",
            labels=_WATER_MODE_LABELS,
            # The panel switches water heating off in its own right, so the
            # select carries an off that writes WaterHeating.Active instead.
            off_param="Active",
        ),
    ),
    # The panel's two ways of putting the water first, from the
    # reverse-engineered schema in daaaaan/truma-inetx-ble: both documented as
    # 0/1 and nothing more, with the timed one carrying a duration beside it.
    # They are not a pair that arrives together -- the gas Combi in #22 and a
    # diesel van both report FasterHeatingMode and neither has BoostMode at
    # all -- so each has its own row and appears only where its own parameter
    # is published.
    ("WaterHeating", "BoostMode"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="water_boost",
            device_class=SwitchDeviceClass.SWITCH,
        ),
    ),
    ("WaterHeating", "FasterHeatingMode"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="faster_water_heating",
            device_class=SwitchDeviceClass.SWITCH,
        ),
    ),
    ("WaterHeating", "FasterHeatingModeTime"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="faster_water_heating_time",
            device_class=SensorDeviceClass.DURATION,
            unit=UnitOfTime.SECONDS,
            # No state class: until it is known whether this counts down or
            # states how long the mode was configured for, a long-term
            # statistic of it would mean nothing.
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # -- energy ----------------------------------------------------------
    # Gas is reflected, never commanded (#16). EnergySrc.GasLevel is writable
    # and a write does go through, but the heater writes it too: on a
    # gas/electric Combi 6 E switching the electric element off moved the gas
    # source on by itself, with nothing sent from here. A switch presented as
    # the user's to own would fight the heater and flap.
    ("EnergySrc", "GasLevel"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="gas",
        ),
    ),
    ("EnergySrc", "DieselLevel"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="diesel",
            device_class=SwitchDeviceClass.SWITCH,
        ),
    ),
    ("EnergySrc", "ElectricLevel"): (
        Row(
            platform=Platform.SELECT,
            translation_key="electric_level",
            labels=_ELECTRIC_LABELS,
        ),
    ),
    # -- air -------------------------------------------------------------
    # AirHeating.Mode is how hard the heater works on the air, not whether it
    # does. Measured on a gas Combi in #22: the panel's own "fast" button
    # writes 0 and normal heating writes 1, with nothing else in the whole
    # parameter dump moving. No "off" -- whether the room is heated at all is
    # the climate entity's business (RoomClimate.Mode), and two controls over
    # one thing would disagree.
    ("AirHeating", "Mode"): (
        Row(
            platform=Platform.SELECT,
            translation_key="air_mode",
            labels=_AIR_MODE_LABELS,
        ),
    ),
    ("AirCirculation", "FanLevel"): (
        Row(
            platform=Platform.NUMBER,
            translation_key="fan_level",
            # Only while the owning device has described no range of its own.
            # 0-10 is a Combi's, and it used to be handed to every device that
            # published the parameter, roof air conditioners included.
            fallback_bounds=(0, 10),
        ),
    ),
    # -- system ----------------------------------------------------------
    ("System", "FlameStatus"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="flame",
            device_class=BinarySensorDeviceClass.RUNNING,
            # Not "anything non-zero". The parameter is type 105, the family
            # the Active parameters belong to, and 2 is the appliance standing
            # by: measured on a Combi 6 E against an independent shore-power
            # meter (#15), it went 1 -> 2 in the same second the draw fell
            # from 1787 W to 105 W. Reporting a flame while the appliance
            # stands by is worse than reporting nothing -- it is the reading
            # an automation acts on.
            on_values=(ActiveState.ACTIVE,),
        ),
        Row(
            platform=Platform.SENSOR,
            translation_key="flame_status",
            # No state class on purpose: 0, 1 and 2 are states, not a
            # quantity, so long-term statistics would average a code into a
            # number that means nothing. It exists so somebody standing next
            # to a running heater can watch the raw value in a history graph.
            entity_category=EntityCategory.DIAGNOSTIC,
            enabled_default=False,
        ),
    ),
    # -- gas bottles -----------------------------------------------------
    # Truma LevelControl sensors, up to two of them, each its own bus device
    # (0x0603 and 0x0604 on the vehicle in #9). They publish the same topic,
    # which is what made a flat model produce one bottle's name beside the
    # other's level. Here they are two devices carrying one row.
    #
    # GasBtl.Name is the user's own label for a bottle ("Links", "Rechts") and
    # has no row: it names the device rather than being a reading, and the
    # device it names is already named after it.
    ("GasBtl", "FillLevelP"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="gas_bottle_level",
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
            precision=0,
        ),
    ),
    # -- the panel's own Bluetooth side ----------------------------------
    #
    # 0x0601 publishes nothing an appliance would -- no temperature, no mode,
    # just the state of the radio this integration reaches the panel over.
    # Which is exactly what is missing when a session wedges: the link is up
    # by everything the host reports and carries nothing, and the panel that
    # could say why has stopped answering, so it cannot be asked then. These
    # put its own side of the link in the recorder *before* the next one.
    #
    # The panel describes none of them with an enum, so none is offered as a
    # named state -- see the flame_status row above for the same reasoning.
    ("BleDeviceManagement", "NrFreeSlots"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="ble_free_slots",
            # The panel publishes a breakdown by device kind, not a count.
            reduce=_free_slots,
            # A count once reduced, so it graphs and averages meaningfully --
            # and the shape of the graph is the question: a panel that refuses
            # new bonds because its list is full got there gradually.
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    ("BleDeviceManagement", "BleConnState"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="ble_conn_state",
            # No state class: a state code, not a quantity.
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    ("BleDeviceManagement", "State"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="ble_mgmt_state",
            # On by default like the two above, though nothing measured says
            # what its values mean yet -- the raw flame value is off for that
            # reason and this is not. A value only worth having *before* the
            # failure it explains has to be recorded before anybody knows to
            # go and enable it, and measured on the van it moves (2 at one
            # session, 1 at the next), so it is not a constant either.
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
}


def rows_for(topic: str, param: str, platform: Platform) -> tuple[Row, ...]:
    """The rows one parameter contributes to one platform."""
    return tuple(
        row for row in ROWS.get((topic, param), ()) if row.platform == platform
    )


def native(row: Row, value: object) -> object:
    """Reduce a structured value, then apply a row's wire scale.

    Anything non-numeric is left alone.

    Rounded to six places, which is far finer than any scale here and exists
    only to keep binary floating point out of the state machine: 137 tenths of
    a volt is 13.7, and ``137 * 0.1`` is 13.700000000000001, which Home
    Assistant would happily record and graph.
    """
    if row.reduce is not None:
        value = row.reduce(value)
    if row.scale is None or not isinstance(value, (int, float)):
        return value
    return round(value * row.scale, 6)
