#!/usr/bin/env python3
"""The climate entity reports its temperatures under the names HA reads.

Home Assistant 2026.11 moved the climate entity's temperatures to ``native_``
names; the old ones are deprecated and stop working in 2027.11. hacs.json
requires 2026.11, so the entity defines only the native names -- and the
deprecated ones must not come back, since defining them is exactly what the
deprecation warning was about.

What it pins, by loading the real climate module:

1. the entity defines the native names and none of the deprecated ones,
2. the values are the appliance's own, in degrees Celsius.

Run: ``python3 tests/test_climate_native_temperature.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201
DEPRECATED = ("current_temperature", "target_temperature", "_attr_temperature_unit")


def _entity():
    stubs.install_homeassistant()
    bus = stubs.load("bus")
    stubs.load("const")
    stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
    stubs.load("profiles")
    stubs.load("entity")
    climate = stubs.load("climate")
    coordinator = stubs.FakeCoordinator(bus.Bus())
    coordinator.report("AirHeating", "Temp", 215, HEATER)
    coordinator.report("AirHeating", "TgtTemp", 220, HEATER)
    return climate.TrumaClimate(coordinator, HEATER)


def test_only_the_native_names_are_defined() -> None:
    own = vars(type(_entity()))
    for name in DEPRECATED:
        assert name not in own, f"defines the deprecated {name}"
    assert own["_attr_native_temperature_unit"] == "°C"


def test_the_values_are_the_appliances_own() -> None:
    entity = _entity()
    assert entity.native_current_temperature == 21.5
    assert entity.native_target_temperature == 22.0


if __name__ == "__main__":
    stubs.run_tests(globals(), "climate native temperature")
