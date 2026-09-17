#!/usr/bin/env python3
"""Offline checks for the energy sources, the batteries and the heat the Combi makes.

No hardware and no Home Assistant install: HA is stubbed and the real
``bus.py``, ``profiles.py``, ``sensor.py``, ``switch.py`` and
``binary_sensor.py`` are loaded against it, then the entities are constructed
and read.

Why this exists:

- **#16.** ``EnergySrc.GasLevel`` was not mapped at all, so a gas/electric
  Combi had no gas reading. It is writable, and a write does go through -- but
  the heater writes it too: measured on that vehicle, switching the electric
  element off moved the gas source on by itself. A switch would fight the
  heater and flap, so gas is reflected and never commanded.
- The same issue exposes the mirror-image bug: the diesel switch was created on
  every vehicle, including gas Combis with no diesel burner and no
  ``EnergySrc.DieselLevel`` at all.
- **#17.** The starter and leisure battery voltages ride on the electrical
  block, in tenths of a volt -- a different scale from ``Eol.Vcc12``, which is
  millivolts.
- **#15 and #24.** ``System.FlameStatus`` takes 0, 1 and 2, and nothing the
  panel publishes says what they mean. Two vehicles have now measured the same
  three: a Combi 6 E against a shore-power meter, where 2 is the appliance
  standing by rather than a second kind of firing, and a Combi 4 gas watched at
  the panel. So the sensor names them instead of showing a code.
- **#27.** And they are not named after a flame. On that same Combi 6 E, with
  the gas switched off at the appliance and the 1800 W element carrying the
  load, the parameter read 1 for seven minutes with nothing burning, and went
  1 -> 2 as the element stopped. Truma's name for the parameter is
  ``FlameStatus``; the entities built from it are Heating and Heating status.

What it pins:

1. gas, diesel and both batteries reach the entities built on them, on the
   device that published them,
2. gas is a read-only reflection -- its row is a binary sensor, and no
   platform writes ``EnergySrc.GasLevel``,
3. each of them is created when, and only when, its parameter is reported,
4. the batteries are scaled by ten, not by a thousand,
5. the heating status is named -- off, running, idle -- and keeps the number
   it was named from, and a value nobody has named reads unknown rather than
   being folded into a state it does not belong in,
6. the heating flag is on while the appliance makes heat and off while it
   merely stands by,
7. and no entity is identified by what it is called: a row's translation_key
   is absent from every unique_id, which is what makes a wrong name cheap to
   correct.

Run: ``python3 tests/test_energy_entities.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

# The electrical block, as measured on the vehicles reported so far. Named
# here only to prove nothing in the source needs to name it.
BOARD = 0x0405
HEATER = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")
BINARY = stubs.load("binary_sensor")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def test_the_params_reach_the_entities_built_on_them() -> None:
    bus = BUS.Bus()
    bus.update("EnergySrc", "GasLevel", 1, HEATER)
    bus.update("EnergySrc", "DieselLevel", 0, HEATER)
    bus.update("VBat", "Voltage", 137, BOARD)
    bus.update("L1Bat", "Voltage", 126, BOARD)

    assert bus.device(HEATER).get("EnergySrc", "GasLevel") == 1
    assert bus.device(HEATER).get("EnergySrc", "DieselLevel") == 0
    assert bus.device(BOARD).get("VBat", "Voltage") == 137
    assert bus.device(BOARD).get("L1Bat", "Voltage") == 126


def test_gas_is_reflected_and_never_commanded() -> None:
    """#16: the heater writes GasLevel itself, so a switch would flap.

    Measured on a gas/electric Combi 6 E: NeedsEnergySrc read 1 throughout and
    switching the electric element off moved the gas source on with nothing
    written from Home Assistant.
    """
    rows = PROFILES.ROWS[("EnergySrc", "GasLevel")]
    assert [row.platform for row in rows] == ["binary_sensor"], (
        "gas was given a control again"
    )

    # ...and no platform names the parameter at all: the table decides, and
    # the table makes it a reading.
    for platform in ("switch", "select", "number", "climate", "binary_sensor"):
        text = (SRC / f"{platform}.py").read_text()
        assert "GasLevel" not in text, f"{platform}.py names EnergySrc.GasLevel"


def test_the_gas_sensor_appears_only_on_a_heater_that_burns_gas() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)
    # The link sensor is not a bus parameter and cannot wait for one.
    assert _keys(made) == ["connection"], made

    coordinator.report("EnergySrc", "GasLevel", 1, HEATER)
    assert _keys(made)[-1] == "gas", made
    assert made[-1].is_on is True
    assert made[-1]._addr == HEATER

    # ...and only once, however many frames follow.
    coordinator.report("EnergySrc", "GasLevel", 0, HEATER)
    assert len(made) == 2, made
    assert made[-1].is_on is False


def test_the_diesel_switch_waits_for_a_diesel_burner() -> None:
    """The mirror image of #16: a gas Combi has no DieselLevel at all."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    assert made == [], "a gas Combi was given a diesel burner switch"

    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    assert _keys(made) == ["diesel"], made
    assert made[0].is_on is True


def test_the_batteries_appear_only_where_something_reports_them() -> None:
    """#17: they come off the electrical block, which most vehicles lack."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    assert "starter_battery_voltage" not in _keys(made)

    coordinator.report("VBat", "Voltage", 137, BOARD)
    coordinator.report("L1Bat", "Voltage", 126, BOARD)
    keys = _keys(made)
    assert "starter_battery_voltage" in keys and "leisure_battery_voltage" in keys, keys
    # They belong to the block that reported them, not to the heater.
    assert _by_key(made, "starter_battery_voltage")._addr == BOARD


def test_the_batteries_are_tenths_of_a_volt_not_millivolts() -> None:
    """137 is 13.7 V. Eol.Vcc12 beside them is millivolts; these are not."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("VBat", "Voltage", 137, BOARD)
    coordinator.report("L1Bat", "Voltage", 126, BOARD)

    assert _by_key(made, "starter_battery_voltage").native_value == 13.7
    assert _by_key(made, "leisure_battery_voltage").native_value == 12.6
    # The supply voltage keeps its own, different scale.
    coordinator.report("Eol", "Vcc12", 13700, HEATER)
    assert _by_key(made, "voltage").native_value == 13.7


def test_the_three_heating_states_are_named() -> None:
    """#15 and #24: two vehicles measured the same three, so name them.

    A Combi 6 E against a shore-power meter (#15) and a Combi 4 gas watched at
    the panel (#24). The second reporter put the idle state plainly: "heater is
    on but room temperature measured is above configured temperature".
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("System", "FlameStatus", 2, HEATER)
    status = _by_key(made, "heating_status")

    assert status._attr_entity_category is stubs.EntityCategory.DIAGNOSTIC
    assert status._attr_device_class == "enum"
    assert status._attr_options == ["off", "running", "idle"]

    for value, state in ((0, "off"), (1, "running"), (2, "idle")):
        coordinator.report("System", "FlameStatus", value, HEATER)
        assert status.native_value == state
        # The number underneath stays visible: it is what made the meaning
        # findable in the first place.
        assert status.extra_state_attributes == {"raw": value}


def test_a_state_nobody_has_named_reads_unknown() -> None:
    """An ENUM sensor may only be in a state it declared.

    Home Assistant rejects anything else from inside the state write, once per
    update forever. Cold ignition and the fan run-on are still unobserved
    (#15), so a fourth value is a thing that can happen -- and when it does it
    has to be visible, not swallowed.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("System", "FlameStatus", 3, HEATER)
    status = _by_key(made, "heating_status")

    assert status.native_value is None
    assert status.extra_state_attributes == {"raw": 3}

    # And it recovers: the unnamed value is reported once, not latched.
    coordinator.report("System", "FlameStatus", 1, HEATER)
    assert status.native_value == "running"


def test_the_heating_states_are_named_in_every_language() -> None:
    import json  # noqa: PLC0415

    for path in [SRC / "strings.json", *sorted((SRC / "translations").glob("*.json"))]:
        entry = json.loads(path.read_text())["entity"]["sensor"]["heating_status"]
        # The state under the word is the one automations match on, so it is
        # the same in every language and only the word moves.
        assert set(entry["state"]) == {"off", "running", "idle"}, path.name


def test_the_heating_flag_is_on_only_while_heat_is_being_made() -> None:
    """#15, measured: 0 off, 1 running, 2 idle -- not 0 off, anything else on.

    On a Combi 6 E the value went 1 -> 2 in the same second shore power fell
    from 1787 W to 105 W. Read as ``> 0``, standing by looked like heating.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)
    coordinator.report("System", "FlameStatus", 0, HEATER)
    heating = _by_key(made, "heating_active")

    assert heating.is_on is False
    coordinator.report("System", "FlameStatus", 1, HEATER)
    assert heating.is_on is True
    coordinator.report("System", "FlameStatus", 2, HEATER)
    assert heating.is_on is False, "2 is the appliance standing by, not heating"
    # A plain flag is still read as "anything non-zero", which is what the
    # tri-state row exists to be different from.
    assert PROFILES.ROWS[("EnergySrc", "GasLevel")][0].on_values is None


def test_the_heating_flag_follows_the_electric_element_too() -> None:
    """#27: the parameter is not about a flame, and the entity is not named one.

    Measured on the Combi 6 E of #15 and #23, gas switched off at the appliance
    from the Truma app and confirmed on the bus, water heating running on Eco
    with the 1800 W element::

        19:38:58   EnergySrc.GasLevel       0     <- gas off
                   EnergySrc.ElectricLevel  2     <- 1800 W
                   shore power              1699 W, flat
        19:45:52   System.FlameStatus       1 -> 2
        19:45:53   shore power                36 W

    Seven minutes at 1 with nothing burning. An entity called Flame reading on
    there is what a gas-flavoured automation keys off.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)
    coordinator.report("EnergySrc", "GasLevel", 0, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 2, HEATER)
    coordinator.report("System", "FlameStatus", 1, HEATER)

    assert _by_key(made, "gas").is_on is False
    assert _by_key(made, "heating_active").is_on is True

    # Nothing anybody reads claims a flame, in any language.
    import json  # noqa: PLC0415

    for path in [SRC / "strings.json", *sorted((SRC / "translations").glob("*.json"))]:
        entity = json.loads(path.read_text())["entity"]
        for domain, key in (
            ("binary_sensor", "heating_active"),
            ("sensor", "heating_status"),
        ):
            name = entity[domain][key]["name"]
            assert "flam" not in name.lower(), (path.name, name)


def test_a_name_is_not_an_identity() -> None:
    """#27: a wrong name has to be correctable, so it cannot be half the id.

    The unique_id used to end in the row's translation_key, which made every
    name permanent -- correcting one orphaned the entity and took its history.
    Identity is the device, the topic, the parameter and the platform now, and
    the platform is only enough because no parameter carries two rows on one
    platform. That is the invariant; it is pinned here rather than guarded in
    the code.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)
    coordinator.report("System", "FlameStatus", 1, HEATER)
    heating = _by_key(made, "heating_active")

    assert "heating" not in heating.unique_id, heating.unique_id
    assert heating.unique_id.endswith("_System.FlameStatus_binary_sensor")

    seen: set[tuple[str, str, str]] = set()
    for (topic, param), rows in PROFILES.ROWS.items():
        for row in rows:
            ident = (topic, param, str(row.platform))
            assert ident not in seen, ident
            seen.add(ident)


def _main() -> None:
    stubs.run_tests(globals(), "energy entities")


if __name__ == "__main__":
    _main()
