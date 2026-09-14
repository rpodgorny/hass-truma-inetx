#!/usr/bin/env python3
"""Offline checks for the water entities and how their writes are addressed.

``bus.py`` needs no stubbing at all -- that is the point of keeping it free of
Home Assistant. The platforms are loaded for real against a stub coordinator,
because "create it on the device that reports it" is the part with actual
logic in it.

Why this exists: the fresh-water pump and the tank levels belong to a device
that is not the heater and not the panel -- an electrical block, at 0x0405 on
the two vehicles reported so far (issues #4, #7 and #8). Its address differs
per vehicle and is renumbered when it is re-paired, so a write cannot be sent
to an address named in the source. And most vehicles have no water hardware at
all, so the entities cannot simply be created for everyone.

What it pins:

1. the water parameters land under the device that published them, and the
   entities built on them read that device rather than the bus,
2. an entity is created when -- and only when -- its parameter is reported,
   on the device that reported it, and only once,
3. a write goes to that same device, and no address for the water hardware is
   written into the source,
4. a topic the panel relays keeps going to the panel,
5. the pump only accepts 0 and 1,
6. every translation key the presentation table and the platforms use has a
   name in strings.json, the water sensors carry an icon (having no device
   class to draw one from), and every non-English translation still covers
   every key English has.

Run: ``python3 tests/test_water_entities.py``
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

PANEL = 0x0101
HEATER = 0x0201
BROKER = 0x0000
# The electrical block, as measured on a Weinsberg and on a second vehicle.
# Named here only to prove nothing in the source needs to name it -- which is
# what the second address is for: the same hardware, re-paired, renumbered.
BOARD = 0x0405
RENUMBERED = 0x0407
# The roof air conditioner on the Combi 6 E of issue #10, for the same reason.
ROOF_AC = 0x0406

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")
SELECT = stubs.load("select")
NUMBER = stubs.load("number")
BINARY = stubs.load("binary_sensor")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _keys(entities) -> list[str]:
    return [entity._attr_translation_key for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def test_water_values_land_under_the_device_that_published_them() -> None:
    bus = BUS.Bus()
    bus.update("FreshWater", "Level", 25, BOARD)
    bus.update("GreyWater", "Level", 0, BOARD)
    bus.update("Switches", "FreshWaterPump", 1, BOARD)

    board = bus.device(BOARD)
    # Levels are a percentage in quarter steps; nothing is scaled on the way
    # in, so a sensor showing 25 must mean the sensor said 25.
    assert board.get("FreshWater", "Level") == 25
    assert board.get("GreyWater", "Level") == 0
    assert board.get("Switches", "FreshWaterPump") == 1
    # Nothing else on the bus acquired them by standing nearby.
    assert bus.device(HEATER).params == {}


def test_an_entity_appears_only_once_its_hardware_reports() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    assert made == [], "created a sensor for hardware that never reported"

    coordinator.report("FreshWater", "Level", 25, BOARD)
    assert _keys(made) == ["fresh_water_level"], "the tank reported and got no sensor"

    # An unrelated parameter must not conjure the rest.
    coordinator.report("Eol", "Vcc12", 13800, HEATER)
    assert _keys(made) == ["fresh_water_level", "voltage"]

    # Repeated reports of the same parameter must not duplicate the entity.
    coordinator.report("FreshWater", "Level", 50, BOARD)
    assert len(made) == 2

    coordinator.report("GreyWater", "Level", 0, BOARD)
    assert "grey_water_level" in _keys(made)


def test_an_entity_belongs_to_the_device_that_reported_it() -> None:
    """Two devices reporting one parameter are two entities, not one."""
    coordinator = _coordinator()
    made = stubs.setup_platform(NUMBER, coordinator)
    coordinator.report("AirCirculation", "FanLevel", 4, HEATER)
    coordinator.report("AirCirculation", "FanLevel", 2, ROOF_AC)

    assert len(made) == 2, "the roof unit's fan overwrote the Combi's again (#9)"
    by_addr = {entity._addr: entity for entity in made}
    assert by_addr[HEATER].native_value == 4
    assert by_addr[ROOF_AC].native_value == 2
    # ...and they are two entities on two devices, not two on one.
    assert (
        by_addr[HEATER]._attr_device_info != by_addr[ROOF_AC]._attr_device_info
    )
    assert by_addr[HEATER]._attr_unique_id != by_addr[ROOF_AC]._attr_unique_id


def test_data_already_in_hand_needs_no_second_pass() -> None:
    """A config-entry reload re-runs setup against a live coordinator."""
    coordinator = _coordinator()
    coordinator.data.update("GreyWater", "Level", 0, BOARD)

    made = stubs.setup_platform(SENSOR, coordinator)

    assert _keys(made) == ["grey_water_level"], (
        "an already-reported tank got no sensor on reload"
    )


def test_a_water_write_goes_to_whoever_owns_the_topic() -> None:
    """The pump's owner differs per vehicle, and changes when it is re-paired."""
    for owner in (BOARD, RENUMBERED):
        coordinator = _coordinator()
        made = stubs.setup_platform(SWITCH, coordinator)
        coordinator.report("Switches", "FreshWaterPump", 0, owner)

        pump = _by_key(made, "water_pump")
        asyncio.run(pump.async_turn_on())
        assert coordinator.writes == [(owner, "Switches", "FreshWaterPump", 1)]

    # No address for the water hardware may be written into the source.
    for name in ("bus.py", "profiles.py", "switch.py", "sensor.py"):
        assert "0x0405" not in (SRC / name).read_text(), f"{name} hardcodes the block"


def test_a_topic_the_panel_relays_still_goes_to_the_panel() -> None:
    """RoomClimate is the panel's own, however the value reaches us."""
    bus = BUS.Bus()
    assert bus.command_dest(HEATER, "RoomClimate") == PANEL
    # Everything else follows the device the entity belongs to.
    for topic in ("WaterHeating", "AirCirculation", "EnergySrc", "AirCooling"):
        assert bus.command_dest(ROOF_AC, topic) == ROOF_AC
    assert set(BUS.COMMAND_DEST) == {"RoomClimate"}, (
        "a destination table that names appliances is what #10 was"
    )
    assert ROOF_AC not in BUS.COMMAND_DEST.values()


def test_the_pump_only_takes_zero_and_one() -> None:
    bus = BUS.Bus()
    assert bus.validate_write(BOARD, "Switches", "FreshWaterPump", 1)[0]
    ok, msg = bus.validate_write(BOARD, "Switches", "FreshWaterPump", 2)
    assert not ok and "2" in msg


def _table_translation_keys() -> dict[str, set[str]]:
    """Every (platform, translation_key) the presentation table declares.

    Read as source rather than imported: the point is to check the table, and
    a key that only exists inside a Home Assistant install is no use to anyone
    reading strings.json.
    """
    text = (SRC / "profiles.py").read_text()
    found: dict[str, set[str]] = {}
    for platform, key in re.findall(
        r"platform=Platform\.([A-Z_]+),\s*\n\s*translation_key=\"([a-z_0-9]+)\"",
        text,
    ):
        found.setdefault(platform.lower(), set()).add(key)
    return found


def _platform_translation_keys(platform: str) -> set[str]:
    """Translation keys a platform file declares on an entity class."""
    text = (SRC / f"{platform}.py").read_text()
    return set(re.findall(r'_attr_translation_key\s*=\s*"([a-z_0-9]+)"', text))


def _leaf_keys(node: object, prefix: str = "") -> set:
    """Every path through a translation file that ends in a string."""
    if not isinstance(node, dict):
        return {prefix}
    return {key for name, value in node.items()
            for key in _leaf_keys(value, f"{prefix}/{name}")}


def test_every_entity_is_named_and_iconed() -> None:
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]

    table = _table_translation_keys()
    assert table, "no rows were found in profiles.py -- the regex has rotted"
    for platform, keys in table.items():
        for key in keys:
            assert key in strings.get(platform, {}), (
                f"{platform}.{key} is a row in profiles.py with no name in "
                "strings.json"
            )

    for platform in ("binary_sensor", "climate", "number", "select", "sensor",
                     "switch"):
        for key in _platform_translation_keys(platform):
            assert key in strings.get(platform, {}), (
                f"{platform}.{key} has no name in strings.json"
            )

    # The water sensors have no device class to take an icon from, so one has
    # to be given; the same for the gas-bottle level.
    for key in ("fresh_water_level", "grey_water_level", "gas_bottle_level"):
        assert key in strings["sensor"], f"sensor.{key} is unnamed"
        assert key in icons["sensor"], (
            f"sensor.{key} has neither icon nor device class"
        )

    en = (SRC / "translations" / "en.json").read_text()
    assert en == (SRC / "strings.json").read_text(), (
        "strings.json and translations/en.json have drifted again"
    )

    # A translation that has lost keys is worse than no translation: Home
    # Assistant falls back per key, so the drift shows up as a German UI with
    # English words scattered through it rather than as an error.
    for path in sorted((SRC / "translations").glob("*.json")):
        if path.name == "en.json":
            continue
        missing = _leaf_keys(json.loads(en)) - _leaf_keys(
            json.loads(path.read_text())
        )
        assert not missing, f"translations/{path.name} is missing {sorted(missing)}"


def test_every_row_names_a_platform_that_is_set_up() -> None:
    """A row on a platform nothing forwards to would never be created."""
    forwarded = set(
        re.findall(r"Platform\.([A-Z_]+),", (SRC / "__init__.py").read_text())
    )
    for (topic, param), rows in PROFILES.ROWS.items():
        for row in rows:
            assert row.platform.upper() in forwarded, (
                f"{topic}.{param} is presented on {row.platform}, which "
                "__init__.py does not forward to"
            )


def _main() -> None:
    stubs.run_tests(globals(), "water entities")


if __name__ == "__main__":
    _main()
