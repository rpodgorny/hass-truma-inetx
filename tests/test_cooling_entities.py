#!/usr/bin/env python3
"""Offline checks for the roof air conditioner and the rest of a gas bottle.

Why this exists: cooling was reachable only as a mode. The panel would accept
``RoomClimate.Mode = 2`` and then the climate entity wrote its setpoint into
``AirHeating.TgtTemp`` on the heater, which is acknowledged by the transport
and changes nothing -- the cooling setpoint is the air conditioner's own
field, on the air conditioner's own device. Measured on a Weinsberg with a
Dometic FreshJet 2200 (2026-09-03, MarioDeMonti's fork): 17 °C set while
cooling put ``AirCooling.TgtTemp`` at 170 and left ``RoomClimate.TgtTemp``
sitting at 270.

What it pins:

1. the air conditioner's own parameters become its own entities, on its own
   device, and the heater gets none of them,
2. the climate setpoint is read from and written to the field the running mode
   actually uses, at the address that publishes it,
3. the setpoint range comes from that field's owner, falling back per topic
   rather than to one range for all three,
4. cooling that is standing by reads as off, the way a heater that is
   standing by does -- and is still visible, as a state of its own on the diagnostic
   sensor beside the flag: measured on the FreshJet of #23, which reports the
   2 that vehicle's first dump had not shown,
5. the air conditioner's own fields are bounded by their own entries,
6. a gas bottle carries its weight, its temperature and its sensor's battery
   as well as its level, each on the bottle that published it,
7. the climate entity reports what the appliance is *doing* -- each mode
   answered by its own function's tri-state, and never by the appliance-wide
   System.FlameStatus, which is ACTIVE with only the boiler working (#30),
8. no device address for any of it is written into the source.

Run: ``python3 tests/test_cooling_entities.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

PANEL = 0x0101
HEATER = 0x0201
# The roof air conditioner and the two gas-bottle sensors of the vehicles in
# #9 and #10. Named here only to prove nothing in the source needs to name
# them: every one of these is renumbered when the device is re-paired.
ROOF_AC = 0x0406
BOTTLE_A = 0x0603
BOTTLE_B = 0x0604

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SELECT = stubs.load("select")
BINARY = stubs.load("binary_sensor")
CLIMATE = stubs.load("climate")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def _climate(coordinator):
    """The climate entity, created the way the platform creates it."""
    made = stubs.setup_platform(CLIMATE, coordinator)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    assert len(made) == 1, made
    return made[0]


def test_the_air_conditioner_gets_its_own_entities() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    selects = stubs.setup_platform(SELECT, coordinator)
    binaries = stubs.setup_platform(BINARY, coordinator)

    coordinator.report("AirCooling", "Temp", 245, ROOF_AC)
    coordinator.report("AirCooling", "Mode", 5, ROOF_AC)
    coordinator.report("AirCooling", "Active", 1, ROOF_AC)

    temp = _by_key(sensors, "cooling_temp")
    assert temp.native_value == 24.5
    assert temp._addr == ROOF_AC, "the cooling reading landed on the heater"
    assert _by_key(selects, "cooling_mode").current_option == "Auto"
    assert _by_key(binaries, "cooling_active").is_on is True

    # The heater publishes none of it and must gain none of it.
    assert BUS.Bus().device(HEATER).params == {}


def test_the_cooling_stages_are_named_the_way_the_unit_names_them() -> None:
    """The panel publishes this enum with names of its own, and 0 is "Low".

    Read off a FreshJet 2200, which enumerates {0: Low, 1: Mid, 2: High,
    3: Max, 4: Night, 5: Auto} (#23). These strings are what automations match
    on, so they follow the unit rather than this repo's word for it -- 0 was
    "Min" here until the unit was asked.
    """
    coordinator = _coordinator()
    selects = stubs.setup_platform(SELECT, coordinator)
    coordinator.report("AirCooling", "Mode", 0, ROOF_AC)

    mode = _by_key(selects, "cooling_mode")
    assert mode.options == ["Low", "Mid", "High", "Max", "Night", "Auto"]
    assert mode.current_option == "Low"


def test_cooling_that_is_standing_by_reads_as_off() -> None:
    """The Active family is tri-state: 2 is the unit idle, not a second on."""
    coordinator = _coordinator()
    binaries = stubs.setup_platform(BINARY, coordinator)
    coordinator.report("AirCooling", "Active", 1, ROOF_AC)
    cooling = _by_key(binaries, "cooling_active")

    assert cooling.is_on is True
    coordinator.report("AirCooling", "Active", 2, ROOF_AC)
    assert cooling.is_on is False, "standing by was reported as cooling"
    coordinator.report("AirCooling", "Active", 0, ROOF_AC)
    assert cooling.is_on is False


def test_standing_by_is_a_state_of_its_own_beside_the_flag() -> None:
    """Measured on the FreshJet of #23: AirCooling.Active really does take 2.

    Cooling selected, the room at 13 °C against a 16 °C target, and the unit
    reporting 2 while the shore-power meter read 336 W against a 311 W base
    load -- awake, compressor off. The flag above cannot say that, and "not
    cooling" and "standing by" are the two an owner wants told apart.
    """
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("AirCooling", "Active", 2, ROOF_AC)
    status = _by_key(sensors, "cooling_status")

    assert status._addr == ROOF_AC
    assert status._attr_entity_category is stubs.EntityCategory.DIAGNOSTIC
    assert status._attr_device_class == "enum"
    assert status._attr_options == ["off", "running", "idle"]

    for value, state in ((0, "off"), (1, "running"), (2, "idle")):
        coordinator.report("AirCooling", "Active", value, ROOF_AC)
        assert status.native_value == state
        assert status.extra_state_attributes == {"raw": value}

    # A value nobody has named stays visible rather than being folded into a
    # state it does not belong in -- an ENUM sensor may only be in a state it
    # declared, and Home Assistant rejects anything else once per update.
    coordinator.report("AirCooling", "Active", 3, ROOF_AC)
    assert status.native_value is None
    assert status.extra_state_attributes == {"raw": 3}


def test_the_cooling_states_are_named_in_every_language() -> None:
    import json  # noqa: PLC0415

    for path in [SRC / "strings.json", *sorted((SRC / "translations").glob("*.json"))]:
        entry = json.loads(path.read_text())["entity"]["sensor"]["cooling_status"]
        # The same three states as the heater's, under the same keys: they are
        # what an automation matches on, so only the words move per language.
        assert set(entry["state"]) == {"off", "running", "idle"}, path.name


def test_the_setpoint_follows_the_running_mode() -> None:
    coordinator = _coordinator()
    climate = _climate(coordinator)
    # All three fields hold a different number, so that a test cannot pass by
    # reading the wrong one.
    coordinator.report("AirHeating", "TgtTemp", 220, HEATER)
    coordinator.report("AirCooling", "TgtTemp", 170, ROOF_AC)
    coordinator.report("RoomClimate", "TgtTemp", 270, PANEL)

    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    assert climate.target_temperature == 22.0
    asyncio.run(climate.async_set_temperature(temperature=21))
    assert coordinator.writes[-1] == (HEATER, "AirHeating", "TgtTemp", 210)

    coordinator.report("RoomClimate", "Mode", 2, PANEL)
    assert climate.target_temperature == 17.0, (
        "cooling showed the heater's setpoint"
    )
    asyncio.run(climate.async_set_temperature(temperature=18))
    assert coordinator.writes[-1] == (ROOF_AC, "AirCooling", "TgtTemp", 180), (
        "the cooling setpoint went to the heater, where it does nothing"
    )

    # Automatic is the panel's own field. This one is an assumption, not a
    # measurement -- it is the field left over, and the panel is what decides
    # in automatic -- so it is pinned here to be found when it turns out wrong.
    coordinator.report("RoomClimate", "Mode", 1, PANEL)
    assert climate.target_temperature == 27.0
    asyncio.run(climate.async_set_temperature(temperature=24))
    assert coordinator.writes[-1] == (PANEL, "RoomClimate", "TgtTemp", 240)

    # Off keeps the heater's resting target, the way it always did.
    coordinator.report("RoomClimate", "Mode", 0, PANEL)
    assert climate.target_temperature == 22.0


def test_a_bus_without_an_air_conditioner_keeps_the_heaters_field() -> None:
    """A mode with no hardware behind it must not lose the setpoint."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.report("AirHeating", "TgtTemp", 220, HEATER)
    coordinator.report("RoomClimate", "Mode", 2, PANEL)

    assert climate.target_temperature == 22.0
    asyncio.run(climate.async_set_temperature(temperature=21))
    assert coordinator.writes[-1] == (HEATER, "AirHeating", "TgtTemp", 210)


def test_the_setpoint_range_belongs_to_the_field() -> None:
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.report("AirCooling", "TgtTemp", 170, ROOF_AC)

    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    assert (climate.min_temp, climate.max_temp) == (5, 30)

    # Nothing cools to 5 °C, and the panel's own slider does not offer it.
    coordinator.report("RoomClimate", "Mode", 2, PANEL)
    assert (climate.min_temp, climate.max_temp) == (16, 30)

    # ...and the unit itself outranks both fallbacks.
    coordinator.describe("AirCooling", "TgtTemp", ROOF_AC, min=180, max=280)
    assert (climate.min_temp, climate.max_temp) == (18, 28)


def test_the_cooling_parameters_are_bounded_by_their_own_entries() -> None:
    """The air conditioner's own fields, which had no entry at all before.

    Not RoomClimate.Mode: cooling reaches the panel there on the strength of
    the panel's own enum (tests/test_panel_declared_options.py), and the
    fallback list stays what this van has.
    """
    bus = BUS.Bus()
    assert bus.validate_write(ROOF_AC, "AirCooling", "TgtTemp", 180)[0]
    assert not bus.validate_write(ROOF_AC, "AirCooling", "TgtTemp", 1800)[0]
    assert bus.validate_write(ROOF_AC, "AirCooling", "Mode", 5)[0]
    assert not bus.validate_write(ROOF_AC, "AirCooling", "Mode", 6)[0]


def test_a_gas_bottle_carries_more_than_its_level() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)

    for addr, level, weight in ((BOTTLE_A, 51, 56), (BOTTLE_B, 100, 110)):
        coordinator.report("GasBtl", "FillLevelP", level, addr)
        coordinator.report("GasBtl", "FillLevelW", weight, addr)
    coordinator.report("GasBtl", "Temperature", 19, BOTTLE_A)
    coordinator.report("BluetoothDevice", "BattLevel", 80, BOTTLE_A)
    coordinator.report("GasBtl", "RemTime", 4200, BOTTLE_A)

    by_addr: dict[int, dict] = {}
    for entity in sensors:
        by_addr.setdefault(entity._addr, {})[entity._attr_translation_key] = entity

    # Kilograms times ten on the wire: the panel showed 5.6 kg at 56.
    assert by_addr[BOTTLE_A]["gas_bottle_contents"].native_value == 5.6
    assert by_addr[BOTTLE_B]["gas_bottle_contents"].native_value == 11.0
    # Whole degrees here, unlike every other temperature on this bus.
    assert by_addr[BOTTLE_A]["gas_bottle_temp"].native_value == 19
    # The sensor's battery is the sensor's, not the panel's.
    assert by_addr[BOTTLE_A]["battery_level"].native_value == 80
    assert "battery_level" not in by_addr[BOTTLE_B]
    # ...and two bottles are two devices, not one that overwrites the other.
    assert (
        by_addr[BOTTLE_A]["gas_bottle_contents"]._attr_device_info
        != by_addr[BOTTLE_B]["gas_bottle_contents"]._attr_device_info
    )
    # The one whose unit nobody knows stays out of the way until somebody
    # watches it move.
    rem = by_addr[BOTTLE_A]["gas_bottle_rem_time"]
    assert rem._attr_entity_registry_enabled_default is False
    assert rem._attr_native_unit_of_measurement is None


def test_the_action_is_what_is_running_not_what_was_asked() -> None:
    """#30: hvac_mode is the instruction, hvac_action is the answer.

    Each mode is answered by the tri-state of the function it drives, which is
    the same resolution the setpoint makes -- heating on the appliance, cooling
    on the unit that cools.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)

    # Heating, on this appliance's own flag.
    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    coordinator.report("AirHeating", "Active", 1, HEATER)
    assert climate.hvac_action == "heating"
    coordinator.report("AirHeating", "Active", 2, HEATER)
    assert climate.hvac_action == "idle", "standing by at target is not heating"
    coordinator.report("AirHeating", "Active", 0, HEATER)
    assert climate.hvac_action == "idle"

    # Off is the mode being off, not a function reporting nothing.
    coordinator.report("RoomClimate", "Mode", 0, PANEL)
    assert climate.hvac_action == "off"

    # Cooling, on the roof unit's flag -- not the heater's, which does not
    # publish it and never will.
    coordinator.report("RoomClimate", "Mode", 2, PANEL)
    assert climate.hvac_action is None, "the unit has said nothing yet"
    coordinator.report("AirCooling", "Active", 1, ROOF_AC)
    assert climate.hvac_action == "cooling"
    coordinator.report("AirCooling", "Active", 2, ROOF_AC)
    assert climate.hvac_action == "idle"

    # Ventilating, on the circulation flag.
    coordinator.report("RoomClimate", "Mode", 5, PANEL)
    coordinator.report("AirCirculation", "Active", 1, HEATER)
    assert climate.hvac_action == "fan"
    coordinator.report("AirCirculation", "Active", 0, HEATER)
    assert climate.hvac_action == "idle"

    # Automatic says which function runs only by running it.
    coordinator.report("RoomClimate", "Mode", 1, PANEL)
    coordinator.report("AirCooling", "Active", 2, ROOF_AC)
    coordinator.report("AirHeating", "Active", 1, HEATER)
    assert climate.hvac_action == "heating"
    coordinator.report("AirCooling", "Active", 1, ROOF_AC)
    assert climate.hvac_action == "cooling"
    coordinator.report("AirCooling", "Active", 0, ROOF_AC)
    coordinator.report("AirHeating", "Active", 2, HEATER)
    assert climate.hvac_action == "idle"


def test_heating_the_water_is_not_heating_the_room() -> None:
    """#30, and #27 one level up: the right value on the wrong thing.

    System.FlameStatus is the appliance making heat, water heating included.
    Taken as the room's action it claims the vehicle is being heated while
    only the boiler works -- the state this repo already has on file, in
    ``dumps/combi4-inetx-pro/water-boost.json``: RoomClimate.Mode 0,
    AirHeating.Active 0, WaterHeating.Active 1, System.FlameStatus 1.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.report("System", "FlameStatus", 1, HEATER)
    coordinator.report("WaterHeating", "Active", 1, HEATER)
    coordinator.report("AirHeating", "Active", 0, HEATER)

    # The dump's own state: the room mode is off while the boiler works.
    coordinator.report("RoomClimate", "Mode", 0, PANEL)
    assert climate.hvac_action == "off"

    # And the harder case the dump does not hold: heating selected, the room
    # already warm, the boiler working anyway. The appliance is making heat;
    # the room is not being heated.
    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    assert climate.hvac_action == "idle", "the boiler is not the room"

    # Which is only true because nothing here consults the appliance-wide
    # flag. Prose may name it -- a read would have to quote it.
    assert '"FlameStatus"' not in (SRC / "climate.py").read_text(), (
        "climate.py reads System.FlameStatus"
    )


def test_no_address_for_any_of_this_is_in_the_source() -> None:
    """Every address here is renumbered when its device is re-paired."""
    for name in ("bus.py", "profiles.py", "climate.py", "sensor.py",
                 "select.py", "binary_sensor.py"):
        text = (SRC / name).read_text()
        for addr in ("0x0406", "0x0603", "0x0604"):
            for line in text.splitlines():
                if addr in line:
                    assert line.lstrip().startswith("#"), (
                        f"{name} hardcodes {addr}: {line.strip()}"
                    )


def _main() -> None:
    stubs.run_tests(globals(), "cooling and gas-bottle entities")


if __name__ == "__main__":
    _main()
