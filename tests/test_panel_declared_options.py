#!/usr/bin/env python3
"""Offline checks for offering what the device says this vehicle has.

No hardware, no Home Assistant install: HA is stubbed and the real ``bus.py``,
``profiles.py``, ``climate.py`` and ``select.py`` are loaded against it, and
the entities are built by the platforms' own setup.

Why this exists. A list of modes written into the source is wrong in both
directions at once. Measured on two vehicles:

- this van's panel enumerates ``RoomClimate.Mode`` as
  ``{0: Off, 3: Heating, 5: Ventilating}`` -- it has no air conditioner, and 1
  and 2 are absent from the enum rather than present-and-unavailable,
- the Combi 6 E in #11 reports 1 = automatic and 2 = cooling on the same
  parameter, verified at the panel.

Offering a fixed ``[0, 3, 5]`` locks that vehicle out of its own air
conditioner; offering everything puts cooling on a van that cannot cool. So the
device is asked. The same goes for the water-heating steps (#12).

And it is asked *per device*, which is the other half. A description is the
property of whichever device gave it: a Combi's fan and a roof air
conditioner's need not run to the same maximum, so a control built from a
merged description offers values its own hardware refuses.

What it pins:

1. the values the owning device enumerates are what gets offered, and one that
   describes nothing still gets the list this integration always had,
2. a value marked unavailable for this vehicle is not offered,
3. the panel's *names* never reach a user-facing string -- they arrive in the
   panel's display language (``40 / 60 / 70`` here, Eco / Comfort / Hot on the
   Combi 6 E), so only the values cross over,
4. a write is validated against that device's enum where there is one, so a
   mode this heater really has stops being rejected by our own table,
5. off is always offered on the climate entity, whatever the panel says,
6. each select is created only on a device that reports its parameter -- a
   Combi D has no electric element and its panel never mentions the parameter,
7. and a slider's range comes from its own device, not from a constant that
   happens to be a Combi's.

Run: ``python3 tests/test_panel_declared_options.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201
ROOF_AC = 0x0406

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
SELECT = stubs.load("select")
NUMBER = stubs.load("number")
CLIMATE = stubs.load("climate")

# The two vehicles this is about, as their panels described themselves.
THIS_VAN = {0: "Off", 3: "Heating", 5: "Ventilating"}
COMBI_6E = {0: "Off", 1: "Automatic", 2: "Cooling", 3: "Heating", 5: "Ventilating"}


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def _enum(names: dict, unavailable: tuple = ()) -> list:
    """An enum in the shape the panel actually sends it."""
    return [
        {"n": name, "a": value not in unavailable, "v": value}
        for value, name in names.items()
    ]


def _climate(coordinator):
    """The climate entity for the heater, created the way the platform does."""
    made = stubs.setup_platform(CLIMATE, coordinator)
    coordinator.report("AirHeating", "Temp", 228, HEATER)
    assert len(made) == 1, made
    return made[0]


def test_a_van_without_cooling_is_not_offered_cooling() -> None:
    """Measured: this panel's enum has no 1 and no 2 in it at all."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.describe("RoomClimate", "Mode", PANEL, v=0, enum=_enum(THIS_VAN))

    assert climate.hvac_modes == ["off", "heat", "fan_only"]


def test_a_vehicle_that_has_cooling_is_offered_it() -> None:
    """The whole of #11: the modes exist, the hardcoded list hid them."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.describe("RoomClimate", "Mode", PANEL, v=0, enum=_enum(COMBI_6E))

    assert climate.hvac_modes == ["off", "auto", "cool", "heat", "fan_only"]


def test_a_silent_panel_still_gets_the_modes_this_always_offered() -> None:
    """Nothing described means fall back, not an empty climate card."""
    assert _climate(_coordinator()).hvac_modes == ["off", "heat", "fan_only"]


def test_a_value_we_have_no_name_for_is_left_out_and_off_stays() -> None:
    """An unknown mode must not become a mode the frontend cannot render."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.describe(
        "RoomClimate", "Mode", PANEL, v=3, enum=_enum({3: "Heating", 9: "Whatever"})
    )

    assert climate.hvac_modes == ["off", "heat"], climate.hvac_modes


def test_the_room_mode_is_read_from_the_panel_that_relays_it() -> None:
    """The heater does not publish RoomClimate; the panel does, for the bus."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    assert climate.hvac_mode is None

    coordinator.report("RoomClimate", "Mode", 3, PANEL)
    assert climate.hvac_mode == "heat"

    # ...and a write to it goes back to the panel, not to the heater the
    # entity belongs to.
    asyncio.run(climate.async_turn_off())
    assert coordinator.writes == [(HEATER, "RoomClimate", "Mode", 0)]
    assert coordinator.data.command_dest(HEATER, "RoomClimate") == PANEL


def test_the_water_steps_come_from_the_panel_but_the_words_do_not() -> None:
    """#12: this panel says 40/60/70, a Combi 6 E says Eco/Comfort/Hot.

    Either way the select shows our own labels, which carry both halves -- the
    panel's own names are in the panel's language, and a select option is
    user-facing text automations match on.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    coordinator.describe(
        "WaterHeating", "Mode", HEATER, v=1,
        enum=_enum({0: "Eco", 1: "Comfort", 2: "Hot"}),
    )

    water = _by_key(made, "water_mode")
    assert water.options == [
        "off", "Eco (40 °C)", "Comfort (60 °C)", "Hot (70 °C)"
    ], water.options


def test_a_step_the_vehicle_does_not_have_is_not_offered() -> None:
    """A panel can describe a value and mark it unavailable here."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    coordinator.describe(
        "EnergySrc", "ElectricLevel", HEATER, v=0,
        enum=_enum({0: "Off", 1: "900 W", 2: "1800 W"}, unavailable=(2,)),
    )

    assert _by_key(made, "electric_level").options == ["off", "900 W"]


def test_a_silent_panel_leaves_the_selects_as_they_were() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    coordinator.report("WaterHeating", "Mode", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)

    assert _by_key(made, "water_mode").options == [
        "off", "Eco (40 °C)", "Comfort (60 °C)", "Hot (70 °C)",
    ]
    assert _by_key(made, "electric_level").options == ["off", "900 W", "1800 W"]


def test_the_water_select_switches_the_function_off_rather_than_the_step() -> None:
    """The panel switches water heating off in its own right."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    coordinator.report("WaterHeating", "Mode", 1, HEATER)
    coordinator.report("WaterHeating", "Active", 1, HEATER)
    water = _by_key(made, "water_mode")

    assert water.current_option == "Comfort (60 °C)"
    coordinator.report("WaterHeating", "Active", 0, HEATER)
    assert water.current_option == "off"

    asyncio.run(water.async_select_option("off"))
    asyncio.run(water.async_select_option("Hot (70 °C)"))
    assert coordinator.writes == [
        (HEATER, "WaterHeating", "Active", 0),
        (HEATER, "WaterHeating", "Active", 1),
        (HEATER, "WaterHeating", "Mode", 2),
    ], coordinator.writes


def test_the_device_outranks_our_table_when_it_has_spoken() -> None:
    """PARAM_VALIDATION says [0, 3, 5]; this vehicle says otherwise."""
    bus = BUS.Bus()
    assert bus.validate_write(PANEL, "RoomClimate", "Mode", 2)[0] is False

    bus.learn_param("RoomClimate", "Mode", {"enum": _enum(COMBI_6E)}, PANEL)
    ok, msg = bus.validate_write(PANEL, "RoomClimate", "Mode", 2)
    assert ok, msg
    # ...and it constrains as well as permits: 4 is in the protocol, not on
    # this vehicle's panel.
    assert bus.validate_write(PANEL, "RoomClimate", "Mode", 4)[0] is False
    # ...and it is that device's claim, not the bus's -- said on a topic
    # nobody relays, which is where that sentence is true. The roof unit
    # describes a four-step fan and refuses a 6; the Combi has described
    # nothing, so it is judged by the table's (0, 10) and takes one.
    bus.learn_param("AirCirculation", "FanLevel", {"min": 0, "max": 4}, ROOF_AC)
    assert bus.validate_write(ROOF_AC, "AirCirculation", "FanLevel", 6)[0] is False
    assert bus.validate_write(HEATER, "AirCirculation", "FanLevel", 6)[0] is True


def test_a_relayed_write_is_judged_by_the_panel_that_receives_it() -> None:
    """#23: cooling was offered by the panel's enum and refused by our table.

    The write goes where COMMAND_DEST sends it, so that is the device whose
    description governs. Validating against the entity's own device asked the
    Combi about RoomClimate -- a topic it does not publish and never will --
    and fell through to PARAM_VALIDATION, written for a van with no air
    conditioner:

        HomeAssistantError: Invalid Truma command:
        RoomClimate.Mode: value 2 not in [0, 3, 5]
    """
    bus = BUS.Bus()
    bus.learn_param("RoomClimate", "Mode", {"enum": _enum(COMBI_6E)}, PANEL)

    ok, msg = bus.validate_write(HEATER, "RoomClimate", "Mode", 2)
    assert ok, msg
    assert bus.write_authority(HEATER, "RoomClimate", "Mode").addr == PANEL

    # The panel's enum constrains such a write as well as permitting it, and
    # the refusal names the device that made the claim rather than the entity
    # the write came from.
    ok, msg = bus.validate_write(HEATER, "RoomClimate", "Mode", 4)
    assert not ok
    assert f"0x{PANEL:04X}" in msg, msg

    # Nothing described at the destination leaves the table in charge, which
    # is what happens while discovery is still running.
    assert BUS.Bus().validate_write(HEATER, "RoomClimate", "Mode", 2)[0] is False


def test_the_climate_entity_can_reach_a_mode_the_panel_offers() -> None:
    """The same bug from the layer the user hit it on (#23).

    The climate entity lives on the heater and offers whatever the panel
    enumerates, so offering cooling and then refusing to write it was the two
    halves of this integration disagreeing about which device to ask.
    """
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.describe("RoomClimate", "Mode", PANEL, v=0, enum=_enum(COMBI_6E))
    assert "cool" in climate.hvac_modes

    asyncio.run(climate.async_set_hvac_mode("cool"))
    assert coordinator.writes == [(HEATER, "RoomClimate", "Mode", 2)], coordinator.writes


def test_our_table_still_applies_where_nothing_was_described() -> None:
    bus = BUS.Bus()
    assert bus.validate_write(HEATER, "Switches", "FreshWaterPump", 1)[0] is True
    assert bus.validate_write(HEATER, "Switches", "FreshWaterPump", 2)[0] is False
    # Ranges keep working too -- those are never enums.
    assert bus.validate_write(PANEL, "RoomClimate", "TgtTemp", 200)[0] is True
    assert bus.validate_write(PANEL, "RoomClimate", "TgtTemp", 900)[0] is False


def test_allowed_values_reports_values_never_names() -> None:
    """The names are evidence for us, not strings for anybody else."""
    bus = BUS.Bus()
    bus.learn_param(
        "WaterHeating", "Mode",
        {"enum": _enum({0: "Eco", 1: "Comfort", 2: "Hot"})}, HEATER,
    )

    heater = bus.device(HEATER)
    assert heater.allowed_values("WaterHeating", "Mode") == [0, 1, 2]
    assert heater.allowed_values("WaterHeating", "Nothing") is None


def test_each_select_waits_for_its_own_parameter() -> None:
    """Measured on a Combi D van: the panel never mentions ElectricLevel.

    That select was offering off / 900 W / 1800 W against hardware that has
    none of them, which reads as a broken integration rather than as absent
    hardware -- the same reasoning the water entities already follow. The
    air-heating mode (#22) is gated for a weaker reason: two panels are known
    to offer Fast / Comfort, but only one has been read in a parameter dump.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    assert made == [], "a select was created for an unmeasured parameter"

    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    assert _keys(made) == ["electric_level"], made

    coordinator.report("AirHeating", "Mode", 1, HEATER)
    assert _keys(made) == ["electric_level", "air_mode"], made

    coordinator.report("WaterHeating", "Mode", 1, HEATER)
    assert _keys(made) == ["electric_level", "air_mode", "water_mode"], made

    # ...and only once each, however many frames follow.
    coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)
    coordinator.report("AirHeating", "Mode", 0, HEATER)
    assert len(made) == 3, made


def test_the_air_mode_reads_back_what_the_heater_reports() -> None:
    """Measured in #22: fast writes 0, normal heating writes 1."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SELECT, coordinator)
    coordinator.report("AirHeating", "Mode", 0, HEATER)
    air = _by_key(made, "air_mode")

    assert air.current_option == "Fast"
    coordinator.report("AirHeating", "Mode", 1, HEATER)
    assert air.current_option == "Comfort"
    # The panel's own names, in the panel's language, never become options.
    coordinator.describe(
        "AirHeating", "Mode", HEATER, v=1,
        enum=_enum({0: "Schnell", 1: "Komfort"}),
    )
    assert air.options == ["Fast", "Comfort"], air.options

    asyncio.run(air.async_select_option("Fast"))
    assert coordinator.writes == [(HEATER, "AirHeating", "Mode", 0)]


def test_a_slider_takes_its_range_from_its_own_device() -> None:
    """0-10 is a Combi's range; it was handed to every fan on the bus."""
    coordinator = _coordinator()
    made = stubs.setup_platform(NUMBER, coordinator)
    coordinator.report("AirCirculation", "FanLevel", 4, HEATER)
    coordinator.describe(
        "AirCirculation", "FanLevel", ROOF_AC, v=2, min=1, max=6,
    )

    by_addr = {entity._addr: entity for entity in made}
    # The Combi described nothing, so it keeps the fallback.
    assert (
        by_addr[HEATER].native_min_value,
        by_addr[HEATER].native_max_value,
    ) == (0, 10)
    assert (
        by_addr[ROOF_AC].native_min_value,
        by_addr[ROOF_AC].native_max_value,
    ) == (1, 6)


def test_the_climate_fan_modes_follow_the_appliance_too() -> None:
    """The fan on the climate card is the same parameter, same argument."""
    coordinator = _coordinator()
    climate = _climate(coordinator)
    coordinator.report("AirCirculation", "FanLevel", 2, HEATER)
    assert climate.fan_modes == ["off", *[str(n) for n in range(1, 11)]]
    assert climate.fan_mode == "2"

    coordinator.describe("AirCirculation", "FanLevel", HEATER, v=2, min=0, max=3)
    assert climate.fan_modes == ["off", "1", "2", "3"]

    asyncio.run(climate.async_set_fan_mode("3"))
    assert coordinator.writes == [(HEATER, "AirCirculation", "FanLevel", 3)]


def _main() -> None:
    stubs.run_tests(globals(), "panel-declared options")


if __name__ == "__main__":
    _main()
