#!/usr/bin/env python3
"""Offline checks for an Alde Compact behind an iNet X panel.

No hardware and no Home Assistant install: HA is stubbed and the real
``bus.py``, ``profiles.py`` and platforms are loaded against it, then fed what
an Alde Compact 3020 HE published on a real bus (iNet X Panel SW 3.5.38).

Why this exists:

- The Alde answers at 0x0404, not at a Combi's 0x0201, and publishes
  ``AirHeating``, ``WaterHeating`` and ``EnergySrc`` there with
  ``Identify.Supplier`` "Alde".
- Its electric element has three 1 kW steps, which its panel enumerates as
  "Electric off" / 1kW / 2kW / 3kW. Under the Combi's labels the shared steps
  read 900 W / 1800 W and 3 kW could not be chosen at all.
- Its gas is an energy source the owner enables at the panel, so it is a
  switch there -- while a Combi's stays a reading (#16).
- Its hot water has no ``WaterHeating.Mode``, so the Combi's water select
  never appears and nothing switched the water.
- It publishes an energy source priority, its outdoor temperature and its own
  heating flag, none of which had a row.

What it pins:

1. an Alde gets the gas switch and not the gas sensor, a Truma heater the
   sensor and not the switch, the Alde-only rows wait for the device to say
   it is an Alde, and a heater that names no supplier keeps the Combi rows,
2. the electric select is named in the Alde's steps, offers 3 kW, and writes
   to the Alde's own address,
3. energy priority, outdoor temperature, the heating flag and the hot-water
   switch appear on the Alde and read and write what it publishes,
4. a Combi is given none of the Alde-only entities.

Run: ``python3 tests/test_alde_entities.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

ALDE = 0x0404
COMBI = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")
SELECT = stubs.load("select")
BINARY = stubs.load("binary_sensor")


def _enum(*names: tuple[int, str]) -> list[dict]:
    return [{"v": value, "n": name, "a": 1} for value, name in names]


def _alde(coordinator: stubs.FakeCoordinator) -> None:
    """What the Compact 3020 HE published, values and descriptions."""
    coordinator.report("Identify", "Name", "Alde Compact 3020 HE", ALDE)
    coordinator.report("Identify", "Supplier", "Alde", ALDE)
    coordinator.report("AirHeating", "Active", 0, ALDE)
    coordinator.report("AirHeating", "Temp", 182, ALDE)
    coordinator.report("AirHeating", "ExtTemp", 149, ALDE)
    coordinator.report("WaterHeating", "Active", 0, ALDE)
    coordinator.report("WaterHeating", "BoostMode", 0, ALDE)
    coordinator.describe(
        "EnergySrc", "ElectricLevel", ALDE, type=2, avail=1, v=1,
        enum=_enum((0, "Electric off"), (1, "1kW"), (2, "2kW"), (3, "3kW")),
    )
    coordinator.describe(
        "EnergySrc", "GasLevel", ALDE, type=2, avail=1, v=0,
        enum=_enum((0, "Gas off"), (1, "Gas on")),
    )
    coordinator.describe(
        "EnergySrc", "EnergySourcePrio", ALDE, type=2, avail=1, v=0,
        enum=_enum((0, "Electric"), (1, "Gas")),
    )


def _combi(coordinator: stubs.FakeCoordinator) -> None:
    coordinator.report("Identify", "Name", "Combi 6 E", COMBI)
    coordinator.report("Identify", "Supplier", "Truma", COMBI)
    coordinator.report("AirHeating", "Active", 0, COMBI)
    coordinator.report("WaterHeating", "Active", 0, COMBI)
    coordinator.report("WaterHeating", "Mode", 0, COMBI)
    coordinator.report("EnergySrc", "GasLevel", 1, COMBI)
    coordinator.report("EnergySrc", "ElectricLevel", 1, COMBI)


def _setup(platform, feed) -> tuple[stubs.FakeCoordinator, list]:
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    made = stubs.setup_platform(platform, coordinator)
    feed(coordinator)
    return coordinator, made


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}: {_keys(entities)}")


def test_an_alde_gets_a_gas_switch_and_no_gas_sensor() -> None:
    coordinator, switches = _setup(SWITCH, _alde)
    gas = _by_key(switches, "gas_switch")
    assert gas._addr == ALDE
    assert gas.is_on is False

    asyncio.run(gas.async_turn_on())
    assert coordinator.writes[-1] == (ALDE, "EnergySrc", "GasLevel", 1)

    _, sensors = _setup(BINARY, _alde)
    assert "gas" not in _keys(sensors), sensors


def test_a_truma_heater_keeps_its_gas_reading_and_gets_no_switch() -> None:
    """#16 still holds wherever the heater is not an Alde."""
    _, sensors = _setup(BINARY, _combi)
    assert _by_key(sensors, "gas")._addr == COMBI

    _, switches = _setup(SWITCH, _combi)
    assert "gas_switch" not in _keys(switches), switches


def test_the_alde_controls_wait_for_the_alde_to_say_what_it_is() -> None:
    """An Alde-only row is built once the device names its supplier.

    A device that names none keeps the rows it always had, so a heater that
    does not publish ``Identify.Supplier`` loses nothing.
    """
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    switches = stubs.setup_platform(SWITCH, coordinator)

    coordinator.report("EnergySrc", "GasLevel", 0, ALDE)
    coordinator.report("WaterHeating", "Active", 0, ALDE)
    assert "gas_switch" not in _keys(switches)
    assert "water_heating" not in _keys(switches)

    coordinator.report("Identify", "Supplier", "Alde", ALDE)
    assert "gas_switch" in _keys(switches)
    assert "water_heating" in _keys(switches)


def test_a_heater_that_names_no_supplier_keeps_the_combi_rows() -> None:
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    sensors = stubs.setup_platform(BINARY, coordinator)
    selects = stubs.setup_platform(SELECT, coordinator)
    coordinator.report("EnergySrc", "GasLevel", 1, COMBI)
    coordinator.report("EnergySrc", "ElectricLevel", 2, COMBI)
    assert _by_key(sensors, "gas").is_on is True
    assert _by_key(selects, "electric_level").current_option == "1800 W"


def test_a_name_without_a_supplier_does_not_settle_the_make() -> None:
    """The order measured on the Compact 3020 HE, mid-startup.

    ``Identify.Name`` names the device, which lets entities be built, and
    ``Identify.Supplier`` came in a later frame. Built in that gap, the Alde
    was given the Combi's gas sensor and the Combi's 900 W / 1800 W select,
    and kept both.
    """
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    coordinator.data.discovered = False
    sensors = stubs.setup_platform(BINARY, coordinator)
    selects = stubs.setup_platform(SELECT, coordinator)
    switches = stubs.setup_platform(SWITCH, coordinator)

    coordinator.report("Identify", "Name", "Alde Compact 3020 HE", ALDE)
    coordinator.report("EnergySrc", "GasLevel", 0, ALDE)
    coordinator.report("EnergySrc", "ElectricLevel", 1, ALDE)
    assert "gas" not in _keys(sensors), sensors
    assert "electric_level" not in _keys(selects), selects

    coordinator.report("Identify", "Supplier", "Alde", ALDE)
    assert "gas" not in _keys(sensors), sensors
    assert "gas_switch" in _keys(switches)
    assert _by_key(selects, "electric_level").current_option == "1 kW"


def test_a_heater_naming_no_supplier_gets_its_rows_when_discovery_ends() -> None:
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    coordinator.data.discovered = False
    sensors = stubs.setup_platform(BINARY, coordinator)

    coordinator.report("Identify", "Name", "Combi 6 E", COMBI)
    coordinator.report("EnergySrc", "GasLevel", 1, COMBI)
    assert "gas" not in _keys(sensors)

    coordinator.data.discovered = True
    coordinator.report("EnergySrc", "GasLevel", 1, COMBI)
    assert _by_key(sensors, "gas").is_on is True


def test_the_alde_electric_select_offers_its_own_three_steps() -> None:
    coordinator, selects = _setup(SELECT, _alde)
    electric = _by_key(selects, "electric_level")
    assert electric.options == ["off", "1 kW", "2 kW", "3 kW"], electric.options
    assert electric.current_option == "1 kW"

    asyncio.run(electric.async_select_option("3 kW"))
    asyncio.run(electric.async_select_option("off"))
    assert coordinator.writes[-2:] == [
        (ALDE, "EnergySrc", "ElectricLevel", 3),
        (ALDE, "EnergySrc", "ElectricLevel", 0),
    ], coordinator.writes


def test_a_combi_keeps_its_wattages() -> None:
    _, selects = _setup(SELECT, _combi)
    assert _by_key(selects, "electric_level").current_option == "900 W"


def test_energy_priority_is_offered_and_written() -> None:
    coordinator, selects = _setup(SELECT, _alde)
    prio = _by_key(selects, "energy_priority")
    assert prio.options == ["Electric", "Gas"], prio.options
    assert prio.current_option == "Electric"

    asyncio.run(prio.async_select_option("Gas"))
    assert coordinator.writes[-1] == (ALDE, "EnergySrc", "EnergySourcePrio", 1)


def test_the_outdoor_temperature_is_tenths_of_a_degree() -> None:
    _, sensors = _setup(SENSOR, _alde)
    outside = _by_key(sensors, "outside_temp")
    assert outside._addr == ALDE
    assert outside.native_value == 14.9


def test_the_heating_flag_reads_the_active_family() -> None:
    """Type 105: 1 is heating, 2 standing by -- not "anything non-zero"."""
    coordinator, sensors = _setup(BINARY, _alde)
    heating = _by_key(sensors, "air_heating_active")
    for value, on in ((0, False), (1, True), (2, False)):
        coordinator.report("AirHeating", "Active", value, ALDE)
        assert heating.is_on is on, (value, heating.is_on)


def test_an_alde_hot_water_is_a_switch_and_has_no_mode_select() -> None:
    coordinator, switches = _setup(SWITCH, _alde)
    water = _by_key(switches, "water_heating")
    assert water.is_on is False

    asyncio.run(water.async_turn_on())
    assert coordinator.writes[-1] == (ALDE, "WaterHeating", "Active", 1)

    _, selects = _setup(SELECT, _alde)
    assert "water_mode" not in _keys(selects), selects


def test_a_combi_is_given_none_of_the_alde_controls() -> None:
    _, switches = _setup(SWITCH, _combi)
    assert "water_heating" not in _keys(switches), switches
    _, selects = _setup(SELECT, _combi)
    assert "water_mode" in _keys(selects)
    assert "energy_priority" not in _keys(selects)


def _main() -> None:
    stubs.run_tests(globals(), "alde entities")


if __name__ == "__main__":
    _main()
