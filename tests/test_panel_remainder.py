#!/usr/bin/env python3
"""Offline checks for what the panel offers beyond its controls.

Taken from the van's own parameter dump (2026-09-16), every device the panel
had let us hear from, read with the panel's own metadata beside each value::

    TimeAndDate.SystemTime   1789574460  [t18 min0 max4294967295]
    Eol.Vcc5                 5058        [t11 min0 max65535]
    Panel.UserInactiveSince  2850        [t18 min0 max4294967295]
    PowerMgmt.PwrMode        3           [t102]
    Temperature.InternalSource 1         [t1 perm0 min0 max255]
    RoomClimate.Active       4           [t107]
    System.DemoMode          0           [t2 enum Off/On/On and configured]
    EnergySrc.NeedsEnergySrc 1           [t104 perm0]   (on the heater)
    ErrorReset.ResetTime     60          [t18 perm0 min0 max60]

What it pins:

1. the panel's clock is read as a UTC epoch and presented as a time, which is
   what the timers fire off,
2. a field whose range is the width of the field rather than a range reads
   unknown at either end instead of graphing 1970,
3. the readings that belong to the panel land on the panel and the ones that
   belong to the appliance land on the appliance,
4. everything here is diagnostic, and the ones that either move constantly or
   mean nothing measured yet are off until somebody asks for them,
5. every key is named and iconed.

Run: ``python3 tests/test_panel_remainder.py``
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

PANEL = 0x0101
HEATER = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
BINARY = stubs.load("binary_sensor")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def test_the_panel_clock_is_read_as_a_time() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    # The moment the van was read: the panel displayed 18:01, and Prague was
    # two hours ahead of UTC that day. So the wire value is plain UTC and the
    # panel does the timezone itself.
    coordinator.describe(
        "TimeAndDate", "SystemTime", PANEL, type=18, min=0, max=4294967295, v=1789574460
    )

    clock = _by_key(sensors, "panel_clock")
    assert clock.native_value == datetime(2026, 9, 16, 16, 1, tzinfo=UTC)
    assert clock._attr_device_class == "timestamp"


def test_a_field_width_is_not_a_time() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("TimeAndDate", "SystemTime", 1789574460, PANEL)
    clock = _by_key(sensors, "panel_clock")

    # A panel that has never been told the time says 0, and 0 is not midnight
    # in 1970 -- it is no reading. The far end of the field is not one either.
    for absurd in (0, 1, 999_999_999):
        coordinator.report("TimeAndDate", "SystemTime", absurd, PANEL)
        assert clock.native_value is None, absurd

    coordinator.report("TimeAndDate", "SystemTime", 1789574460, PANEL)
    assert clock.native_value is not None


def test_the_panels_readings_land_on_the_panel() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    binaries = stubs.setup_platform(BINARY, coordinator)

    coordinator.describe("Eol", "Vcc5", PANEL, type=11, min=0, max=65535, v=5058)
    coordinator.report("Panel", "UserInactiveSince", 2850, PANEL)
    coordinator.report("PowerMgmt", "PwrMode", 3, PANEL)
    coordinator.report("Temperature", "InternalSource", 1, PANEL)
    coordinator.report("RoomClimate", "Active", 4, PANEL)
    coordinator.report("System", "DemoMode", 0, PANEL)

    # Millivolts, like the 12 V rail beside it.
    assert _by_key(sensors, "logic_supply").native_value == 5.058
    assert _by_key(sensors, "panel_idle").native_value == 2850
    assert _by_key(sensors, "power_mode").native_value == 3
    assert _by_key(sensors, "temperature_source").native_value == 1
    # Read 4 while heating, which is a value the appliance-level Active
    # parameters have not been seen at -- so the raw number, not a flag.
    assert _by_key(sensors, "climate_state").native_value == 4

    demo = _by_key(binaries, "demo_mode")
    assert demo.is_on is False
    # "On and configured" is the panel's own second on-value, and it is on.
    coordinator.report("System", "DemoMode", 2, PANEL)
    assert demo.is_on is True

    for key in ("logic_supply", "panel_idle", "power_mode", "temperature_source"):
        assert _by_key(sensors, key)._addr == PANEL


def test_the_appliances_readings_land_on_the_appliance() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    binaries = stubs.setup_platform(BINARY, coordinator)

    coordinator.report("EnergySrc", "NeedsEnergySrc", 1, HEATER)
    coordinator.describe(
        "ErrorReset", "ResetTime", HEATER, type=18, perm=0, min=0, max=60, v=60
    )

    needs = _by_key(binaries, "needs_energy")
    assert needs.is_on is True
    assert needs._addr == HEATER

    window = _by_key(sensors, "fault_reset_window")
    assert window.native_value == 60
    assert window._addr == HEATER
    assert window._attr_native_unit_of_measurement == "s"


def test_none_of_it_clutters_the_device_page() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    binaries = stubs.setup_platform(BINARY, coordinator)

    coordinator.describe(
        "TimeAndDate", "SystemTime", PANEL, type=18, min=0, max=4294967295, v=1789574460
    )
    coordinator.describe("Eol", "Vcc5", PANEL, type=11, min=0, max=65535, v=5058)
    coordinator.report("Panel", "UserInactiveSince", 2850, PANEL)
    coordinator.report("PowerMgmt", "PwrMode", 3, PANEL)
    coordinator.report("Temperature", "InternalSource", 1, PANEL)
    coordinator.report("RoomClimate", "Active", 4, PANEL)
    coordinator.report("System", "DemoMode", 0, PANEL)
    coordinator.report("EnergySrc", "NeedsEnergySrc", 1, HEATER)
    coordinator.describe(
        "ErrorReset", "ResetTime", HEATER, type=18, perm=0, min=0, max=60, v=60
    )

    made = {
        key: _by_key(sensors + binaries, key)
        for key in (
            "panel_clock",
            "logic_supply",
            "panel_idle",
            "power_mode",
            "temperature_source",
            "climate_state",
            "fault_reset_window",
            "demo_mode",
            "needs_energy",
        )
    }
    for key, entity in made.items():
        assert entity._attr_entity_category == "diagnostic", key

    # On by default: a fault the appliance is raising, a vehicle whose
    # readings are staged rather than measured, and the clock the timers fire
    # off are all things somebody has to be able to see without being told to
    # go and enable an entity first.
    on_by_default = {"panel_clock", "demo_mode", "needs_energy"}
    for key, entity in made.items():
        want = key in on_by_default
        assert entity._attr_entity_registry_enabled_default is want, key


def test_every_new_key_is_named_and_iconed() -> None:
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]
    keys = {
        ("sensor", "panel_clock"),
        ("sensor", "logic_supply"),
        ("sensor", "panel_idle"),
        ("sensor", "power_mode"),
        ("sensor", "temperature_source"),
        ("sensor", "climate_state"),
        ("sensor", "fault_reset_window"),
        ("binary_sensor", "demo_mode"),
        ("binary_sensor", "needs_energy"),
    }
    for platform, key in keys:
        assert key in strings[platform], f"{key} has no name"
        assert key in icons[platform], f"{key} has no icon"

    for path in sorted((SRC / "translations").glob("*.json")):
        entity = json.loads(path.read_text())["entity"]
        for platform, key in keys:
            assert key in entity[platform], f"{path.name} is missing {key}"


def test_nothing_here_offers_to_break_the_vehicle() -> None:
    # The panel publishes these too, and they are deliberately not rows: a
    # factory reset with no undo behind one tap, a firmware flash over a BLE
    # link that drops when the van is driven, and the demo mode that would
    # stage every reading in the vehicle. Demo mode is read above and not
    # written; the other two are not presented at all.
    import truma_pkg.profiles as profiles  # noqa: PLC0415

    for topic, param in (
        ("System", "FactoryReset"),
        ("Install", "InstallNow"),
        ("Install", "Status"),
        ("Transfer", "Status"),
    ):
        assert (topic, param) not in profiles.ROWS, f"{topic}.{param} is presented"

    # Demo mode is presented, and only as something to read.
    from homeassistant.const import Platform  # noqa: PLC0415

    demo = profiles.ROWS[("System", "DemoMode")]
    assert [row.platform for row in demo] == [Platform.BINARY_SENSOR]


stubs.run_tests(globals(), "the rest of the panel")
