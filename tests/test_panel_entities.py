#!/usr/bin/env python3
"""Offline checks for the panel's own entities, the fault flag and the timer.

These come from the only public parameter dump taken on a third vehicle -- a
Combi 4 gas behind an iNet X panel, 107 parameters with the panel's own
metadata, attached to #22 -- plus the shore-power reading on the Weinsberg of
#17. Nothing here is guessed from a name: every value below was read off that
dump.

What it pins:

1. mains power is reported by the panel as well as by an electrical block, and
   where both report it they are two entities on two devices rather than one
   that overwrites the other,
2. the fault flag reduces the appliance's error list before reading it, so an
   empty list is healthy and a non-empty one is a problem, and the fault code
   beside it carries what the appliance said about that fault,
3. the reset button is offered only by an appliance raising a fault it calls
   resettable, and presses at that appliance's own address,
4. the timer switch writes the panel's own timer state,
5. the panel's display controls are configuration, and their range is the
   panel's own -- except where the panel describes the width of the field
   instead of a range, which is clipped rather than offered,
6. every one of them is named and iconed.

Run: ``python3 tests/test_panel_entities.py``
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

SRC = stubs.SRC

PANEL = 0x0101
HEATER = 0x0201
BOARD = 0x0405

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
stubs.load("profiles")
stubs.load("entity")
NUMBER = stubs.load("number")
BUTTON = stubs.load("button")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")
BINARY = stubs.load("binary_sensor")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


def test_mains_power_is_read_from_whoever_reports_it() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)

    # Most vehicles have only this one: every panel publishes it, and an
    # electrical block is hardware most vans do not carry.
    coordinator.report("System", "Plugged", 0, PANEL)
    panel_side = _by_key(made, "line_power")
    assert panel_side.is_on is False
    coordinator.report("System", "Plugged", 1, PANEL)
    assert panel_side.is_on is True

    # A vehicle with a block reports it twice, and two sources are two
    # entities -- on two devices, so they are told apart.
    coordinator.report("LinePower", "Plugged", 1, BOARD)
    both = [e for e in made if e._attr_translation_key == "line_power"]
    assert len(both) == 2, "the block's answer replaced the panel's"
    assert {e._addr for e in both} == {PANEL, BOARD}
    assert len({e._attr_unique_id for e in both}) == 2


def test_the_fault_flag_counts_the_error_list() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(BINARY, coordinator)
    coordinator.report("ErrorReset", "ErrCode", [], HEATER)
    fault = _by_key(made, "error")

    assert fault.is_on is False, "an empty error list is a healthy appliance"
    coordinator.report("ErrorReset", "ErrCode", [{"code": 1}], HEATER)
    assert fault.is_on is True
    # Anything that is not a list at all is unknown rather than healthy.
    coordinator.report("ErrorReset", "ErrCode", 0, HEATER)
    assert fault.is_on is None


# What the heater raised on the van with the window above it open, read off
# the bus at 15:23 on 2026-09-16.
WINDOW_OPEN = [{"sev": 1, "code": 412, "resettable": 0}]


def test_the_fault_code_carries_what_the_appliance_said() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    coordinator.report("ErrorReset", "ErrCode", WINDOW_OPEN, HEATER)
    code = _by_key(sensors, "error_code")

    assert code.native_value == 412, "the code somebody looks up in the manual"
    assert code.extra_state_attributes == {
        "count": 1,
        "errors": WINDOW_OPEN,
        "severity": 1,
        "resettable": False,
    }
    assert code._addr == HEATER

    # An appliance can raise more than one, and the state can only be one of
    # them: the list is carried whole, in the same shape it had for one.
    both = WINDOW_OPEN + [{"sev": 2, "code": 500, "resettable": 1}]
    coordinator.report("ErrorReset", "ErrCode", both, HEATER)
    assert code.native_value == 412, "the state is the one the appliance listed first"
    assert code.extra_state_attributes == {
        "count": 2,
        "errors": both,
        "severity": 1,
        "resettable": False,
    }

    # Healthy is no code at all -- 0 would be a code nobody can look up -- and
    # the attributes still say so rather than disappearing.
    coordinator.report("ErrorReset", "ErrCode", [], HEATER)
    assert code.native_value is None
    assert code.extra_state_attributes == {"count": 0, "errors": []}

    # A fault that is not the one in the state can still be the clearable one,
    # which is why the button reads the list and not these attributes.
    coordinator.report("ErrorReset", "ErrCode", both, HEATER)
    assert code.extra_state_attributes["resettable"] is False
    assert any(e["resettable"] for e in code.extra_state_attributes["errors"])


def test_the_reset_button_waits_for_a_fault_that_can_be_reset() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(BUTTON, coordinator)
    # The parameter the panel's own reset writes. Measured on the van, the
    # heater publishes it and the panel does not.
    coordinator.report("ErrorReset", "Req", 0, HEATER)
    button = _by_key(made, "error_reset")
    coordinator.data.connected = True

    assert button.available is False, "offered a reset with nothing to reset"

    coordinator.report("ErrorReset", "ErrCode", WINDOW_OPEN, HEATER)
    assert button.available is False, (
        "a fault the appliance calls unresettable was offered a reset anyway"
    )

    coordinator.report(
        "ErrorReset", "ErrCode", [{"sev": 2, "code": 500, "resettable": 1}], HEATER
    )
    assert button.available is True

    asyncio.run(button.async_press())
    assert coordinator.writes == [(HEATER, "ErrorReset", "Req", 1)]


def test_the_timer_can_be_switched_off_from_here() -> None:
    coordinator = _coordinator()
    made = stubs.setup_platform(SWITCH, coordinator)
    # Timer1State read 1 on the panel of #22 with a timer armed. Reported
    # bare, with no metadata: a panel that says nothing about whether the slot
    # is available still gets its switch, because a control withheld on a
    # guess is invisible. The six slots and the avail flag that picks between
    # them are in test_timer_entities.py.
    coordinator.report("TimerConfig", "Timer1State", 1, PANEL)

    timer = _by_key(made, "timer")
    assert timer.is_on is True
    asyncio.run(timer.async_turn_off())
    assert coordinator.writes == [(PANEL, "TimerConfig", "Timer1State", 0)]


def test_the_display_controls_are_the_panels_own_range() -> None:
    coordinator = _coordinator()
    numbers = stubs.setup_platform(NUMBER, coordinator)
    switches = stubs.setup_platform(SWITCH, coordinator)

    # As the panel described them.
    coordinator.describe("Panel", "Intst", PANEL, min=10, max=100, v=100)
    coordinator.describe("Panel", "DarkIntst", PANEL, min=1, max=10, v=1)
    coordinator.report("Panel", "Screensaver", 0, PANEL)

    bright = _by_key(numbers, "panel_brightness")
    assert (bright.native_min_value, bright.native_max_value) == (10, 100)
    assert bright.native_value == 100
    asyncio.run(bright.async_set_native_value(40))
    assert coordinator.writes[-1] == (PANEL, "Panel", "Intst", 40)

    night = _by_key(numbers, "panel_night_brightness")
    assert (night.native_min_value, night.native_max_value) == (1, 10)

    # Configuration, not a reading: these belong on the panel's settings, not
    # in the middle of the heating controls.
    for entity in (bright, night, _by_key(switches, "panel_screensaver")):
        assert entity._attr_entity_category == "config", entity


def test_a_range_that_is_a_field_width_is_clipped() -> None:
    """The panel describes its display timeout as 0 to 4294967295 seconds."""
    coordinator = _coordinator()
    numbers = stubs.setup_platform(NUMBER, coordinator)
    coordinator.describe(
        "Panel", "DisplayTimeout", PANEL, min=0, max=4294967295, v=120
    )
    timeout = _by_key(numbers, "panel_display_timeout")

    assert (timeout.native_min_value, timeout.native_max_value) == (0, 600)
    assert timeout.native_value == 120

    # It only ever narrows: a panel that describes less keeps its own answer.
    coordinator.describe("Panel", "DisplayTimeout", PANEL, min=30, max=300)
    assert (timeout.native_min_value, timeout.native_max_value) == (30, 300)

    # ...and nothing else is clipped, because nothing else asked to be.
    coordinator.describe("AirCirculation", "FanLevel", HEATER, min=0, max=10, v=4)
    fan = [e for e in numbers if e._attr_translation_key == "fan_level"][0]
    assert (fan.native_min_value, fan.native_max_value) == (0, 10)


def test_every_new_entity_is_named_and_iconed() -> None:
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]
    de = json.loads((SRC / "translations" / "de.json").read_text())["entity"]

    for platform, key, needs_icon in (
        ("binary_sensor", "line_power", False),  # device class draws one
        ("binary_sensor", "error", False),
        ("switch", "timer", True),
        ("switch", "panel_screensaver", True),
        ("number", "panel_brightness", True),
        ("number", "panel_night_brightness", True),
        ("number", "panel_display_timeout", True),
        ("sensor", "error_code", True),
        ("button", "error_reset", True),
    ):
        assert key in strings.get(platform, {}), f"{platform}.{key} has no name"
        assert key in de.get(platform, {}), f"{platform}.{key} is not translated"
        if needs_icon:
            assert key in icons.get(platform, {}), (
                f"{platform}.{key} has no device class and no icon either"
            )


def _main() -> None:
    stubs.run_tests(globals(), "panel entities, faults and the timer")


if __name__ == "__main__":
    _main()
