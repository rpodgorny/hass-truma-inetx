#!/usr/bin/env python3
"""Offline checks for the two water-priority modes and their duration.

No hardware and no Home Assistant install: HA is stubbed and the real
``bus.py``, ``profiles.py``, ``switch.py`` and ``sensor.py`` are loaded
against it, then the entities are constructed and read.

Why this exists: the panel offers a mode that puts the burner's whole output
into the boiler, and the reverse-engineered schema in
`daaaaan/truma-inetx-ble` shows two parameters that could be it --
``WaterHeating.BoostMode`` and ``WaterHeating.FasterHeatingMode``, both
documented as 0/1 and nothing else, with ``FasterHeatingModeTime`` (seconds)
beside the second. The two vehicles read so far -- the gas Combi in #22 and a
diesel van -- both have ``FasterHeatingMode`` and ``FasterHeatingModeTime`` and
no ``BoostMode`` at all, so they are not even a pair that arrives together, and
on a vehicle shaped like those the panel's boost can only be the timed one.

That is the whole reason for these checks. Both are offered so the vehicle can
say which one it has, and neither may be created before its own parameter has
been reported -- otherwise a heater with only one of them, or neither, is given
a switch that writes into nothing.

What it pins:

1. all three parameters land under the heater that published them,
2. each switch appears when, and only when, its own parameter is reported --
   one arriving must not conjure the other,
3. the writes carry the parameter's own name and 0/1, and are addressed to the
   device that published it rather than to an address named in the source,
4. the duration is reported raw, in seconds, as a diagnostic with no state
   class -- nothing yet says whether it counts down.

Run: ``python3 tests/test_water_priority.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201
# The same heater after a re-pairing renumbered it. Named to prove the write
# follows the device rather than a constant.
RENUMBERED = 0x0203

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def test_the_three_parameters_land_under_the_heater() -> None:
    bus = BUS.Bus()
    bus.update("WaterHeating", "BoostMode", 1, HEATER)
    bus.update("WaterHeating", "FasterHeatingMode", 0, HEATER)
    bus.update("WaterHeating", "FasterHeatingModeTime", 1800, HEATER)

    heater = bus.device(HEATER)
    assert heater.get("WaterHeating", "BoostMode") == 1
    assert heater.get("WaterHeating", "FasterHeatingMode") == 0
    assert heater.get("WaterHeating", "FasterHeatingModeTime") == 1800


def test_neither_switch_exists_until_its_own_parameter_arrives() -> None:
    """A heater that never mentions one gets nothing for it."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    assert made == [], "a control was created for an unmeasured parameter"

    coordinator.report("WaterHeating", "BoostMode", 0, HEATER)
    assert _keys(made) == ["water_boost"], made
    assert made[0].is_on is False

    # One arriving must not conjure the other: they may well not both exist.
    coordinator.report("WaterHeating", "FasterHeatingModeTime", 1800, HEATER)
    assert _keys(made) == ["water_boost"], made

    coordinator.report("WaterHeating", "FasterHeatingMode", 1, HEATER)
    assert _keys(made) == ["water_boost", "faster_water_heating"], made
    assert made[1].is_on is True

    # ...and only once, however many frames follow.
    coordinator.report("WaterHeating", "BoostMode", 1, HEATER)
    assert len(made) == 2, made
    assert made[0].is_on is True


def test_the_combi_in_22_gets_the_faster_switch_and_no_boost_switch() -> None:
    """Both vehicles read so far, as their dumps have it.

    ``WaterHeating.FasterHeatingMode`` at 0 with ``FasterHeatingModeTime`` at
    0 beside it, and no ``BoostMode`` anywhere in the parameter dump. This is
    the case the per-parameter gate exists for: pairing them, or creating both
    once one arrives, would put a boost switch on a heater whose panel has
    never mentioned the parameter it writes.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)

    coordinator.report("WaterHeating", "FasterHeatingMode", 0, HEATER)
    coordinator.report("WaterHeating", "FasterHeatingModeTime", 0, HEATER)

    assert _keys(made) == ["faster_water_heating"], made
    assert made[0].is_on is False


def test_each_switch_writes_its_own_parameter_to_its_own_device() -> None:
    for owner in (HEATER, RENUMBERED):
        coordinator = _coordinator()
        made = stubs.setup_platform(SWITCH, coordinator)
        coordinator.report("WaterHeating", "BoostMode", 0, owner)
        coordinator.report("WaterHeating", "FasterHeatingMode", 0, owner)
        boost = _by_key(made, "water_boost")
        faster = _by_key(made, "faster_water_heating")

        asyncio.run(boost.async_turn_on())
        asyncio.run(boost.async_turn_off())
        asyncio.run(faster.async_turn_on())
        asyncio.run(faster.async_turn_off())

        assert coordinator.writes == [
            (owner, "WaterHeating", "BoostMode", 1),
            (owner, "WaterHeating", "BoostMode", 0),
            (owner, "WaterHeating", "FasterHeatingMode", 1),
            (owner, "WaterHeating", "FasterHeatingMode", 0),
        ], coordinator.writes

    bus = BUS.Bus()
    for param in ("BoostMode", "FasterHeatingMode"):
        ok, _ = bus.validate_write(HEATER, "WaterHeating", param, 1)
        assert ok
        ok, msg = bus.validate_write(HEATER, "WaterHeating", param, 2)
        assert not ok and "2" in msg, f"{param} accepted a value outside 0/1"


def test_the_duration_is_reported_raw_in_seconds() -> None:
    """Nothing says whether it counts down, so it is shown, not interpreted."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    assert "faster_water_heating_time" not in _keys(made)

    coordinator.report("WaterHeating", "FasterHeatingModeTime", 1800, HEATER)
    timer = _by_key(made, "faster_water_heating_time")

    assert timer.native_value == 1800, "the duration was scaled on the way out"
    assert timer._attr_native_unit_of_measurement == "s"
    assert timer._attr_entity_category is stubs.EntityCategory.DIAGNOSTIC
    assert timer._attr_state_class is None, (
        "a statistic of a value that may be a countdown means nothing"
    )


def _main() -> None:
    stubs.run_tests(globals(), "water priority")


if __name__ == "__main__":
    _main()
