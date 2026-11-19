#!/usr/bin/env python3
"""The climate entity reports its temperatures under the names HA reads.

Why (Home Assistant 2026.11, in the log of every install): the climate
entity's ``current_temperature``, ``target_temperature`` and
``_attr_temperature_unit`` are deprecated in favour of ``native_`` names, and
stop working in 2027.11 -- which would leave the room climate with no reading
and no setpoint. hacs.json still declares 2024.12, where only the old names
exist, so both kinds of Home Assistant have to be served.

What it pins, by loading the real climate module against each:

1. on a Home Assistant with the native names, the entity defines those and
   none of the deprecated ones (defining them is what the warning is about),
2. on one without, the old names are there and read the same values,
3. either way the values are the appliance's, in degrees Celsius.

Run: ``python3 tests/test_climate_native_temperature.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201
DEPRECATED = ("current_temperature", "target_temperature", "_attr_temperature_unit")


def _load_climate(native: bool):
    """The real climate module, built against a ClimateEntity of either age."""
    stubs.install_homeassistant()
    if native:

        class ClimateEntity:
            """Just enough of 2026.11's base class to be recognised."""

            native_current_temperature = None
            native_target_temperature = None

        sys.modules["homeassistant.components.climate"].ClimateEntity = ClimateEntity
    bus = stubs.load("bus")
    stubs.load("const")
    stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
    stubs.load("profiles")
    stubs.load("entity")
    return bus, stubs.load("climate")


def _entity(bus_mod, climate_mod):
    coordinator = stubs.FakeCoordinator(bus_mod.Bus())
    coordinator.report("AirHeating", "Temp", 215, HEATER)
    coordinator.report("AirHeating", "TgtTemp", 220, HEATER)
    return climate_mod.TrumaClimate(coordinator, HEATER)


def test_a_native_home_assistant_gets_only_the_native_names() -> None:
    bus_mod, climate_mod = _load_climate(native=True)
    own = vars(climate_mod.TrumaClimate)
    for name in DEPRECATED:
        assert name not in own, f"defines the deprecated {name}"
    assert own["_attr_native_temperature_unit"] == "°C"

    entity = _entity(bus_mod, climate_mod)
    assert entity.native_current_temperature == 21.5
    assert entity.native_target_temperature == 22.0


def test_an_older_home_assistant_still_reads_the_old_names() -> None:
    bus_mod, climate_mod = _load_climate(native=False)
    own = vars(climate_mod.TrumaClimate)
    assert own["_attr_temperature_unit"] == "°C"
    assert "_attr_native_temperature_unit" not in own

    entity = _entity(bus_mod, climate_mod)
    assert entity.current_temperature == 21.5
    assert entity.target_temperature == 22.0
    assert entity.current_temperature == entity.native_current_temperature


if __name__ == "__main__":
    stubs.run_tests(globals(), "climate native temperature")
