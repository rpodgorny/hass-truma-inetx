#!/usr/bin/env python3
"""Offline checks that an entity waits for its device to be named.

No hardware, no Home Assistant install: HA is stubbed and the real
``entity.py``, ``sensor.py`` and ``climate.py`` are loaded against it, so the
platforms' own setup decides what gets built and when.

Why this exists (#23, measured on a Combi 6 E + iNet X Pro with a roof air
conditioner, an electrical block and two gas-bottle sensors). Home Assistant
builds an entity id out of its device's name at the moment the entity is
created, and never revises it. A bus device is registered as ``Bus device
0xNNNN`` until ``Identify.Name`` arrives -- and the name arrives *late*,
because subscribing makes the panel push values while the descriptions that
carry the name are not asked for until later in startup. So nine of about
seventy entities came up like this and stayed:

    climate.bus_device_0x0201
    sensor.bus_device_0x0405_storungscode
    number.bus_device_0x0406_lufterstufe

while their siblings on the same devices came up ``combi_6_e_*``,
``ebl25x_5_*`` and ``freshjet_6_*``. A race, not a rule, and the owner's only
fix was renaming nine entities by hand.

What it pins:

1. a value alone does not build an entity -- the device's name is what is
   waited for, and the wait is not recorded as done,
2. the entity is built at the update that settles the name, on both creation
   paths (the table's, and the per-device one the climate entity uses),
3. nothing waits forever: when discovery finishes, a device that has named
   itself nothing gets its entities under the address it is at,
4. the owner's own label settles it too, for a device that publishes no
   Identify at all.

Run: ``python3 tests/test_device_naming_race.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201
ROOF_AC = 0x0406
BOTTLE = 0x0603

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
CLIMATE = stubs.load("climate")


def _coordinator() -> stubs.FakeCoordinator:
    """A coordinator whose startup has *not* finished, which is the window."""
    coordinator = stubs.FakeCoordinator(BUS.Bus())
    coordinator.data.discovered = False
    return coordinator


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def test_a_value_alone_does_not_build_an_entity() -> None:
    """The evidence the hardware exists is not evidence of what it is called."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("AirHeating", "Temp", 228, HEATER)

    assert made == [], _keys(made)

    # ...and the wait was not recorded as done: the entity is built at the
    # update that settles the name, not skipped for the life of the session.
    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    assert "current_temp" in _keys(made), _keys(made)


def test_the_climate_entity_waits_the_same_way() -> None:
    """The path ``climate.bus_device_0x0201`` came off (#23).

    The climate entity is not one parameter, so it is built by
    ``async_add_per_device`` rather than from the table -- and that path had
    the same race.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(CLIMATE, coordinator)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    assert made == []

    coordinator.report("Identify", "Name", "Combi 6 E", HEATER)
    assert len(made) == 1, made
    assert made[0]._addr == HEATER


def test_the_owners_label_settles_it_too() -> None:
    """A gas-bottle sensor publishes its label with its first reading.

    Which is why the two ``Truma LevelControl`` devices on that vehicle never
    lost their names: the label arrives in the same burst as the level, so
    there is no window to lose.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("GasBtl", "Name", "Links", BOTTLE)
    coordinator.report("GasBtl", "FillLevelP", 49, BOTTLE)

    assert "gas_bottle_level" in _keys(made), _keys(made)


def test_nothing_waits_past_the_end_of_discovery() -> None:
    """A device that names itself nothing still gets its entities.

    0x0601 publishes BleDeviceManagement and no Identify at all, and an
    electrical block need not name itself either. Once every device has been
    asked to describe itself, the address is the final answer rather than a
    value still in flight -- so the wait ends there.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("AirCooling", "Temp", 245, ROOF_AC)
    assert made == []

    coordinator.data.discovered = True
    coordinator.report("AirCooling", "Temp", 245, ROOF_AC)
    assert "cooling_temp" in _keys(made), _keys(made)


def test_the_panel_never_waits() -> None:
    """The hub is named after the config entry, not off the bus."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("RoomClimate", "Active", 4, PANEL)

    assert "climate_state" in _keys(made), _keys(made)


def _main() -> None:
    stubs.run_tests(globals(), "device naming race")


if __name__ == "__main__":
    _main()
