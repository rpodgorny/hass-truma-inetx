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
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    Platform,
    UnitOfElectricPotential,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)

from .bus import ActiveState

# Wire scale for a temperature: the protocol carries tenths of a degree.
TENTHS = 0.1

# Display precision for a duration the wire carries as whole seconds.
#
# Stated rather than left to Home Assistant, which infers a precision from the
# device class when a sensor offers none and lands on two decimals for a
# duration in seconds -- so a panel that had been untouched for 2850 seconds
# read "2850.00", which is two digits of resolution the parameter does not
# have. It has rather less than one: measured on the van, the idle counter
# moves in steps of ten seconds, so the honest precision is -1. Home Assistant
# does not take one -- it clamps its own inferred precisions at 0 (see
# ``_calculate_precision_from_ratio``) -- and 0 shows every one of these
# parameters exactly as the device sent it, which is the point.
_WHOLE_SECONDS = 0


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

    # Whether the parameter arriving is enough on its own. Normally it is --
    # see ``async_add_rows``. A panel is the exception: it publishes all six
    # of its timer slots whether or not anything is in them, and says which
    # are filled in the `avail` flag of each one. A row that opts in here is
    # created only once its device stops saying the parameter is unavailable,
    # which for the timers means a slot appears when a timer is put in it.
    requires_avail: bool = False

    # The values a translated name interpolates, given the parameter's own
    # name. For a row that a device publishes several numbered copies of: one
    # row, one translation key, one name per copy ("Timer 3"), and the number
    # comes from the wire name rather than from six near-identical rows.
    placeholders: Callable[[str], dict[str, str]] | None = None

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
    # number: a range to clip the device's own description into. The device is
    # the authority on what it accepts and that does not change here -- but a
    # panel that describes its display timeout as 0 to 4294967295 seconds is
    # stating the width of the field, not offering a control, and a slider
    # 136 years long is no control either.
    bounds_limit: tuple[int, int] | None = None

    # The attributes a row publishes beside its state, built from the raw wire
    # value. For a parameter whose value is a structure the row reduces to one
    # number: the number is the state, and what the structure also said is not
    # worth a second entity.
    attrs: Callable[[Any], dict] | None = None

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


def _has_error(value: object) -> int | None:
    """Whether anything is in the appliance's error list.

    ``ErrorReset.ErrCode`` is the codes currently raised: an empty list on a
    healthy appliance, measured on a Combi 4 (#22). That is all this reads out
    of it. What a code means is not known here, and a number nobody can look
    up is worse than a flag that says to go and read the panel -- the codes
    themselves are in a diagnostics download, unchanged.
    """
    if not isinstance(value, list):
        return None
    return 1 if value else 0


def _first_error(value: object) -> dict | None:
    """The first entry of an error list, or None if there is not one."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _error_code(value: object) -> int | None:
    """The code of the fault an appliance is currently raising.

    Measured on the van with the window above the heater open, which is a
    fault it raises at once::

        [{"sev": 1, "code": 412, "resettable": 0}]

    The first entry, not a joined string: the state is what somebody looks up
    in the manual, and the rest of the list is beside it in the attributes. A
    healthy appliance reports an empty list and this reads unknown -- there is
    no code, and 0 would be a code nobody can look up.
    """
    first = _first_error(value)
    code = first.get("code") if first else None
    return code if isinstance(code, int) else None


def _error_attrs(value: object) -> dict:
    """Every fault the appliance is raising, and what it says about the first.

    An appliance can raise more than one at a time, and the state can only be
    one of them, so the whole list is here -- always, and always in the same
    shape. An earlier version carried it only when there was more than one
    fault, which made an automation reading these attributes handle two
    shapes and see neither on a healthy appliance.

    ``severity`` and ``resettable`` describe the fault in the state, which is
    the first one the appliance listed. Not the most severe: nothing measured
    says which way ``sev`` runs, and ordering the list by a guess would put a
    code in the state that the appliance did not put first. ``resettable`` is
    per fault, which is why the reset button reads the list rather than this
    (see button.py) -- one of several faults can be clearable while the one in
    the state is not.
    """
    if not isinstance(value, list):
        return {}
    attrs: dict = {"count": len(value), "errors": value}
    first = _first_error(value)
    if first is None:
        return attrs
    if isinstance(first.get("sev"), int):
        attrs["severity"] = first["sev"]
    if first.get("resettable") is not None:
        attrs["resettable"] = bool(first["resettable"])
    return attrs


def _epoch(value: object) -> datetime | None:
    """A wire epoch as an aware datetime, or None if it is not a time.

    The parameters that carry one describe themselves as 0 to 4294967295,
    which is the width of the field rather than a range: 0 is a panel that
    has never been told the time, not midnight in 1970, and either end of
    that field would be graphed as a real reading. So anything before 2001 is
    read as "no time", and the rest is UTC -- measured on the van, where
    SystemTime 1789574460 stood against a panel displaying 18:01 in Prague.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < 1_000_000_000:
        return None
    return datetime.fromtimestamp(value, UTC)


# The panel's timers. Measured on the van, where one is configured::
#
#     TimerConfig.Timer1      {"id": 1, "name": "23 °C", "symbol": 1,
#                              "start": "06:30", "end": "",
#                              "wd": [1, 1, 1, 1, 1, 1, 1]}   type 206 perm 0
#     TimerConfig.Timer1State 1                               type 119
#     TimerConfig.Timer2..6   the same shape, all zeros, avail 0
#
# So: six fixed slots, each a structure the panel fills in and a flag saying
# whether that slot is armed. The flag carries no ``perm`` and is therefore
# writable (see ``Device.writable``), which is what the switch writes. The
# structure carries ``perm`` 0 -- the panel describes the timer itself as
# read-only, the way it describes its serial number and its clock -- so what
# a timer *does* is shown here and set on the panel.
TIMER_SLOTS = range(1, 7)


def _timer_slot(param: str) -> dict[str, str]:
    """The slot number out of ``Timer3`` or ``Timer3State``, to name it."""
    return {"slot": param.removeprefix("Timer").removesuffix("State")}


def _timer_start(value: object) -> str | None:
    """When the timer starts, which is the one thing worth a state.

    An unfilled slot carries ``""`` and reads unknown rather than an empty
    string that looks like a reading. The end time is often empty on a filled
    slot too -- the measured one starts at 06:30 and never ends -- so it is an
    attribute beside the rest, not the state.
    """
    if not isinstance(value, dict):
        return None
    start = value.get("start")
    return start if isinstance(start, str) and start else None


def _timer_attrs(value: object) -> dict:
    """Everything else the panel said about the timer.

    The same keys every time, so what reads them never has to ask which shape
    it got. ``weekdays`` is passed through as the panel's own seven flags: the
    only timer measured so far repeats on all seven days, which tells nothing
    about which end of the list is Monday, and naming the days on a guess
    would put a wrong day into an automation invisibly.
    """
    if not isinstance(value, dict):
        return {}
    return {
        "timer_name": value.get("name"),
        "start": value.get("start"),
        "end": value.get("end"),
        "weekdays": value.get("wd"),
        "symbol": value.get("symbol"),
    }


# Labels. Defined beside the rows that use them so a value and its name cannot
# drift apart.
# Eco / Comfort / Hot are the panel's own names for the three stages; the
# temperatures say what each one means. A second vehicle's owner reads them as
# stages only and holds that the 40/60/70 does not match a Combi 6 E
# (2026-09-03) -- recorded here rather than acted on, because these strings are
# what automations match on and they have already been renamed once.
_WATER_MODE_LABELS = {0: "Eco (40 °C)", 1: "Comfort (60 °C)", 2: "Hot (70 °C)"}
_ELECTRIC_LABELS = {0: "off", 1: "900 W", 2: "1800 W"}
_AIR_MODE_LABELS = {0: "Fast", 1: "Comfort"}
# A roof air conditioner's own stages, measured on a Dometic FreshJet 2200 by
# switching all six at the panel one at a time (2026-09-02). Not a thermostat
# mode: it is how hard the unit runs, which is why "Auto" sits inside it.
_COOLING_LABELS = {0: "Min", 1: "Mid", 2: "High", 3: "Max", 4: "Night", 5: "Auto"}

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
    # Shore power as the electrical block sees it -- a second, independent
    # answer to a question many vehicles already answer with a smart plug, and
    # worth having precisely because the two can disagree.
    ("LinePower", "Plugged"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="line_power",
            device_class=BinarySensorDeviceClass.PLUG,
        ),
    ),
    # The panel's own answer to the same question, and the one most vehicles
    # have: LinePower belongs to an electrical block, which most vans do not
    # carry, while every panel publishes this. Measured on two vehicles -- 0
    # with the van unplugged (#22) and 1 on shore power (#17) -- and named the
    # same as the block's, because it is the same fact. Where both exist they
    # are two entities on two devices, which is what two sources are.
    ("System", "Plugged"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="line_power",
            device_class=BinarySensorDeviceClass.PLUG,
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
    # The panel's "refill" button. While it is on, the tank sensor measures
    # continuously and the panel sounds a long tone at full; the rest of the
    # time it measures only now and then, which is the whole reason a level
    # lags behind a fill (#4, and see session.request_measurements, which asks
    # for one reading rather than leaving this on). Measured on a Weinsberg,
    # 2026-09-04. Not a switch to leave on: outside a fill there is nothing to
    # watch.
    ("FreshWater", "Autofill"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="water_autofill",
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
            precision=_WHOLE_SECONDS,
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
    # -- cooling ---------------------------------------------------------
    # A roof air conditioner is its own device on the bus -- a Dometic
    # FreshJet 2200 at 0x0406 on the Weinsberg of #10 -- and it publishes its
    # own topic. Whether the room is cooled at all is still RoomClimate on the
    # panel, which is the climate entity's HVACMode.COOL; what is here is what
    # belongs to the unit itself.
    ("AirCooling", "Temp"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="cooling_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTemperature.CELSIUS,
            scale=TENTHS,
        ),
    ),
    # AirCooling.TgtTemp deliberately has no row: it is the cooling setpoint,
    # and the climate entity already owns the setpoint of whichever mode is
    # running (see climate.py). Two controls over one thing would disagree --
    # the same reasoning that keeps AirHeating.TgtTemp out of this table.
    ("AirCooling", "Mode"): (
        Row(
            platform=Platform.SELECT,
            translation_key="cooling_mode",
            labels=_COOLING_LABELS,
        ),
    ),
    ("AirCooling", "Active"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="cooling_active",
            device_class=BinarySensorDeviceClass.RUNNING,
            # Read the way the tri-state Active family reads, so a 2 is the
            # unit standing by rather than a second kind of "on" -- the care
            # FlameStatus needed. A plain 0/1 flag reads identically either
            # way, so this costs nothing if it turns out to be one.
            on_values=(ActiveState.ACTIVE,),
        ),
    ),
    # -- system ----------------------------------------------------------
    # Whether the appliance is reporting a fault at all, and which fault it
    # is. Empty on the healthy Combi 4 of #22; the flag was mapped before the
    # bus rewrite and lost in it, and the code is what a manual is looked up
    # with.
    ("ErrorReset", "ErrCode"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="error",
            device_class=BinarySensorDeviceClass.PROBLEM,
            reduce=_has_error,
        ),
        Row(
            platform=Platform.SENSOR,
            translation_key="error_code",
            # No device class and no state class: a fault code is a name, not
            # a quantity, and averaging one would mean nothing.
            reduce=_error_code,
            attrs=_error_attrs,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # Clearing a fault the appliance says can be cleared. ErrorReset.Req is
    # the parameter the panel's own reset writes; the button appears on the
    # device that publishes it and is available only while that device is
    # raising something resettable -- see button.py.
    ("ErrorReset", "Req"): (
        Row(
            platform=Platform.BUTTON,
            translation_key="error_reset",
            entity_category=EntityCategory.CONFIG,
        ),
    ),
    # How many of the six slots are armed, in one reading -- the one an
    # automation asks rather than adding up six switches. Measured on the van
    # at 1, with one timer configured and enabled, min 0 max 255.
    ("TimerConfig", "TimerEnableCount"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="timers_enabled",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # The six timer slots themselves are generated below, being six copies of
    # two rows.
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
    # -- the panel's display ---------------------------------------------
    # Configuration rather than readings: the panel describes all four as
    # writable and gives its own range for each (#22). Dimming the panel at
    # night is the obvious use, and it is the one thing on the bus that is
    # about the panel rather than about the vehicle.
    ("Panel", "Intst"): (
        Row(
            platform=Platform.NUMBER,
            translation_key="panel_brightness",
            unit=PERCENTAGE,
            entity_category=EntityCategory.CONFIG,
            fallback_bounds=(10, 100),
        ),
    ),
    ("Panel", "DarkIntst"): (
        Row(
            platform=Platform.NUMBER,
            translation_key="panel_night_brightness",
            # 1 to 10 on the panel measured, which is not the percentage the
            # daytime one is, so it carries no unit rather than a wrong one.
            entity_category=EntityCategory.CONFIG,
            fallback_bounds=(1, 10),
        ),
    ),
    ("Panel", "DisplayTimeout"): (
        Row(
            platform=Platform.NUMBER,
            translation_key="panel_display_timeout",
            unit=UnitOfTime.SECONDS,
            entity_category=EntityCategory.CONFIG,
            step=10,
            fallback_bounds=(0, 600),
            # The panel describes 0 to 4294967295 here -- the width of the
            # field, not a control. Ten minutes is past any display timeout
            # worth setting; the panel measured sits at 120.
            bounds_limit=(0, 600),
        ),
    ),
    ("Panel", "Screensaver"): (
        Row(
            platform=Platform.SWITCH,
            translation_key="panel_screensaver",
            device_class=SwitchDeviceClass.SWITCH,
            entity_category=EntityCategory.CONFIG,
        ),
    ),
    # -- gas bottles -----------------------------------------------------
    # Truma LevelControl sensors, up to two of them, each its own bus device
    # (0x0603 and 0x0604 on the vehicle in #9). They publish the same topic,
    # which is what made a flat model produce one bottle's name beside the
    # other's level. Here they are two devices carrying one row.
    #
    # GasBtl.Name is the owner's own label for a bottle, set at the panel, and
    # has no row: it names the device rather than being a reading. Nothing
    # reads it today -- a bottle is named from Identify.Name plus its instance
    # ("Truma LevelControl", "Truma LevelControl 2"), which tells two apart
    # without being either one's name. Naming the device from it instead is a
    # decision about device naming, not a row, and belongs in
    # TrumaCoordinator.device_info.
    ("GasBtl", "FillLevelP"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="gas_bottle_level",
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
            precision=0,
        ),
    ),
    ("GasBtl", "FillLevelW"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="gas_bottle_contents",
            device_class=SensorDeviceClass.WEIGHT,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfMass.KILOGRAMS,
            # Kilograms times ten on the wire: 56 while the panel showed
            # 5.6 kg for the same bottle, read off the two together.
            scale=TENTHS,
            precision=1,
        ),
    ),
    ("GasBtl", "Temperature"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="gas_bottle_temp",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfTemperature.CELSIUS,
            # Whole degrees, unlike every other temperature on this bus. The
            # bottle sensor is a Bluetooth device of its own and does not
            # share the heater's tenths.
        ),
    ),
    ("GasBtl", "RemTime"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="gas_bottle_rem_time",
            # No unit and no device class on purpose. The name says time; the
            # panel shows a percentage and a weight and no time at all, and
            # nothing measured says which this is. Calling it hours would be
            # an invention, so it is off by default and carries the raw
            # number for whoever watches it move.
            entity_category=EntityCategory.DIAGNOSTIC,
            enabled_default=False,
        ),
    ),
    # The bottle sensor's own battery, published under BluetoothDevice rather
    # than under GasBtl. In a flat reading that was ambiguous -- it could as
    # easily have been the panel's -- and filed under the device that sent it,
    # it is the sensor's, on the same device page as the level it explains.
    ("BluetoothDevice", "BattLevel"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="battery_level",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            unit=PERCENTAGE,
            precision=0,
            entity_category=EntityCategory.DIAGNOSTIC,
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
    # -- what is left of the panel ---------------------------------------
    #
    # The panel's own clock, which is what its timers fire off. Measured on
    # the van: TimeAndDate.SystemTime 1789574460 while the panel displayed
    # 18:01 in Prague, so the parameter is a plain UTC epoch and the panel
    # does the timezone itself. Worth seeing because the integration writes
    # this clock at every connect (see ``build_identity_frames``) and a timer
    # firing an hour out is otherwise a mystery with nothing to look at.
    ("TimeAndDate", "SystemTime"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="panel_clock",
            device_class=SensorDeviceClass.TIMESTAMP,
            reduce=_epoch,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # The panel's 5 V logic rail, beside the 12 V supply above. Off by
    # default for the same reason: it is a number to read when something
    # else has already gone wrong.
    ("Eol", "Vcc5"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="logic_supply",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            unit=UnitOfElectricPotential.VOLT,
            scale=0.001,
            precision=2,
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # Showroom mode. The panel names its own values -- Off / On / On and
    # configured -- and in either "on" the readings are staged rather than
    # measured. Read, never written: a vehicle in demo mode reports a
    # plausible temperature from a heater that is not running, and somebody
    # has to be able to see that from here. Turning it *on* from Home
    # Assistant serves nobody.
    ("System", "DemoMode"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="demo_mode",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # How long since anybody touched the panel, in seconds. Off by default:
    # it counts up on every update, so enabling it writes a state every poll
    # forever, and what it answers -- is somebody at the panel right now --
    # is worth that only to whoever asks for it.
    ("Panel", "UserInactiveSince"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="panel_idle",
            device_class=SensorDeviceClass.DURATION,
            unit=UnitOfTime.SECONDS,
            precision=_WHOLE_SECONDS,
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # Published by the panel and by the heater, read 3 on both. Nothing
    # measured says what the values mean, so it is the raw number, off by
    # default, recorded for whoever next has a power question.
    ("PowerMgmt", "PwrMode"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="power_mode",
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # Which sensor the panel is taking its room temperature from. Read 1 on
    # the van, and the panel offers external and internal probes -- so this
    # is the parameter that answers "why does the room temperature not match
    # the room", and the raw value is what there is until one is measured
    # against the other.
    ("Temperature", "InternalSource"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="temperature_source",
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # The panel's own climate state, in the family the Active parameters
    # belong to. Read 4 on the van while heating, which is a value none of
    # the appliance-level Active parameters has been seen at -- so it is the
    # raw number rather than a flag, off by default, the same treatment the
    # raw flame value gets.
    ("RoomClimate", "Active"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="climate_state",
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # Whether the appliance is asking for energy right now -- measured 1 on
    # the van with the heater running on diesel. On the appliance, not the
    # panel: it is the appliance that wants the gas or the diesel.
    ("EnergySrc", "NeedsEnergySrc"): (
        Row(
            platform=Platform.BINARY_SENSOR,
            translation_key="needs_energy",
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
    # How long the appliance will keep accepting a fault reset, in seconds:
    # 60 on the van, against a WaitTime of 900 it publishes as unavailable.
    # Off by default -- it is only ever read beside the reset button.
    ("ErrorReset", "ResetTime"): (
        Row(
            platform=Platform.SENSOR,
            translation_key="fault_reset_window",
            device_class=SensorDeviceClass.DURATION,
            unit=UnitOfTime.SECONDS,
            precision=_WHOLE_SECONDS,
            enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
    ),
}


# Six slots, two rows each: the switch that arms the timer, and the sensor
# that says what arming it does. Written as a loop rather than as twelve
# entries because they differ only in the number, which is also how they are
# named -- see ``placeholders``.
#
# Both opt into ``requires_avail``: the panel publishes all six slots always,
# and a vehicle with one timer would otherwise get five switches that arm
# nothing and five schedules reading unknown. Filling a slot at the panel
# makes its pair appear at the next update. Emptying one leaves them behind,
# because entities are never removed -- the switch then reads off and the
# schedule keeps the times it last had.
for _slot in TIMER_SLOTS:
    ROWS[("TimerConfig", f"Timer{_slot}State")] = (
        Row(
            platform=Platform.SWITCH,
            translation_key="timer",
            device_class=SwitchDeviceClass.SWITCH,
            placeholders=_timer_slot,
            requires_avail=True,
        ),
    )
    ROWS[("TimerConfig", f"Timer{_slot}")] = (
        Row(
            platform=Platform.SENSOR,
            translation_key="timer_schedule",
            reduce=_timer_start,
            attrs=_timer_attrs,
            placeholders=_timer_slot,
            requires_avail=True,
        ),
    )


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
