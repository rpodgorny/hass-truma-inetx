#!/usr/bin/env python3
"""Offline checks for the panel's six timer slots.

Measured on the van (2026-09-16), with one timer configured in the first slot
and nothing in the other five::

    TimerConfig.Timer1      {"id": 1, "name": "23 °C", "symbol": 1,
                             "start": "06:30", "end": "",
                             "wd": [1, 1, 1, 1, 1, 1, 1]}   type 206 perm 0
    TimerConfig.Timer1State 1                               type 119 avail 1
    TimerConfig.Timer2..6   the same shape, all zeros,      type 206 avail 0
    TimerConfig.TimerEnableCount 1                          min 0 max 255

What it pins:

1. a panel publishes all six slots always, and only the filled ones become
   entities -- an empty slot is not a timer that is off, it is not a timer,
2. a slot filled after startup gets its entities at that update, without a
   restart,
3. the switch arms and disarms its own slot,
4. the schedule reads the start time and carries the rest beside it, in the
   same shape whether or not the panel filled every field,
5. each pair is named by its slot number,
6. every key used here is named and iconed.

Run: ``python3 tests/test_timer_entities.py``
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

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")
SWITCH = stubs.load("switch")

# The van's own timer, as the panel published it.
MORNING = {
    "id": 1,
    "name": "23 °C",
    "symbol": 1,
    "start": "06:30",
    "end": "",
    "wd": [1, 1, 1, 1, 1, 1, 1],
}
# What an empty slot carries. Note that it is not blank: it is a whole
# structure of zeros, which is why the avail flag is the thing to read.
EMPTY = {
    "id": 0,
    "name": "DefaultTimer",
    "symbol": 7,
    "start": "00:00",
    "end": "00:00",
    "wd": [0, 0, 0, 0, 0, 0, 0],
}


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _by_param(entities, param: str):
    for entity in entities:
        if getattr(entity, "_param", None) == param:
            return entity
    raise AssertionError(f"no entity for {param}")


def _publish_panel(coordinator, filled: int = 1) -> None:
    """Publish all six slots, the first ``filled`` of them with a timer."""
    for slot in range(1, 7):
        armed = slot <= filled
        coordinator.describe(
            "TimerConfig",
            f"Timer{slot}",
            PANEL,
            type=206,
            perm=0,
            avail=1 if armed else 0,
            v=MORNING if armed else EMPTY,
        )
        coordinator.describe(
            "TimerConfig",
            f"Timer{slot}State",
            PANEL,
            type=119,
            avail=1 if armed else 0,
            v=1 if armed else 0,
        )


def test_an_empty_slot_is_not_a_timer() -> None:
    coordinator = _coordinator()
    switches = stubs.setup_platform(SWITCH, coordinator)
    sensors = stubs.setup_platform(SENSOR, coordinator)
    _publish_panel(coordinator)

    timers = [e for e in switches if e._topic == "TimerConfig"]
    schedules = [e for e in sensors if e._param.startswith("Timer")]
    assert [e._param for e in timers] == ["Timer1State"], (
        "five empty slots were offered as switches that arm nothing"
    )
    assert [e._param for e in schedules] == ["Timer1"]


def test_a_slot_filled_later_appears_without_a_restart() -> None:
    coordinator = _coordinator()
    switches = stubs.setup_platform(SWITCH, coordinator)
    sensors = stubs.setup_platform(SENSOR, coordinator)
    _publish_panel(coordinator)

    # A second timer put in at the panel: the slot stops being unavailable and
    # starts carrying something.
    _publish_panel(coordinator, filled=2)

    assert [e._param for e in switches if e._topic == "TimerConfig"] == [
        "Timer1State",
        "Timer2State",
    ]
    assert [e._param for e in sensors if e._param.startswith("Timer")] == [
        "Timer1",
        "Timer2",
    ]


def test_the_switch_arms_its_own_slot() -> None:
    coordinator = _coordinator()
    switches = stubs.setup_platform(SWITCH, coordinator)
    _publish_panel(coordinator, filled=3)

    third = _by_param(switches, "Timer3State")
    assert third.is_on is True
    asyncio.run(third.async_turn_off())
    assert coordinator.writes == [(PANEL, "TimerConfig", "Timer3State", 0)]

    coordinator.report("TimerConfig", "Timer3State", 0, PANEL)
    assert third.is_on is False
    asyncio.run(third.async_turn_on())
    assert coordinator.writes[-1] == (PANEL, "TimerConfig", "Timer3State", 1)


def test_the_schedule_says_when_and_carries_the_rest() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    _publish_panel(coordinator)

    schedule = _by_param(sensors, "Timer1")
    assert schedule.native_value == "06:30"
    assert schedule.extra_state_attributes == {
        "timer_name": "23 °C",
        "start": "06:30",
        "end": "",
        # Passed through as the panel's seven flags. The one timer measured so
        # far repeats every day, which says nothing about which end is Monday,
        # and a guessed day would reach an automation invisibly.
        "weekdays": [1, 1, 1, 1, 1, 1, 1],
        "symbol": 1,
    }

    # A panel that fills a slot but leaves the start blank reads unknown rather
    # than an empty string that looks like a time.
    coordinator.report("TimerConfig", "Timer1", dict(MORNING, start=""), PANEL)
    assert schedule.native_value is None
    assert schedule.extra_state_attributes["end"] == ""


def test_each_pair_is_named_by_its_slot() -> None:
    coordinator = _coordinator()
    switches = stubs.setup_platform(SWITCH, coordinator)
    sensors = stubs.setup_platform(SENSOR, coordinator)
    _publish_panel(coordinator, filled=6)

    for slot in range(1, 7):
        arm = _by_param(switches, f"Timer{slot}State")
        schedule = _by_param(sensors, f"Timer{slot}")
        assert arm._attr_translation_placeholders == {"slot": str(slot)}
        assert schedule._attr_translation_placeholders == {"slot": str(slot)}
        # One translation key for all six, so the unique_id of the slot that
        # has always been mapped does not move and take its history with it.
        assert arm._attr_translation_key == "timer"
        assert arm._attr_unique_id.endswith(f"TimerConfig.Timer{slot}State_timer")


def test_the_count_of_armed_timers_is_readable() -> None:
    coordinator = _coordinator()
    sensors = stubs.setup_platform(SENSOR, coordinator)
    coordinator.describe(
        "TimerConfig", "TimerEnableCount", PANEL, type=1, perm=0, min=0, max=255, v=1
    )

    count = _by_param(sensors, "TimerEnableCount")
    assert count.native_value == 1
    assert count._attr_entity_category == "diagnostic"


def test_the_panel_calls_the_timer_itself_read_only() -> None:
    # Which is why nothing here writes a timer's times, days or name: the
    # panel describes the structure with perm 0, the way it describes its
    # serial number and its clock, and a write is refused before it is sent.
    coordinator = _coordinator()
    _publish_panel(coordinator)

    ok, why = coordinator.data.validate_write(PANEL, "TimerConfig", "Timer1", 1)
    assert ok is False
    assert "read-only" in why

    # The flag beside it carries no perm at all, and is written.
    ok, _ = coordinator.data.validate_write(PANEL, "TimerConfig", "Timer1State", 0)
    assert ok is True


def test_every_timer_key_is_named_and_iconed() -> None:
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]
    keys = {
        ("switch", "timer"),
        ("sensor", "timer_schedule"),
        ("sensor", "timers_enabled"),
    }
    for platform, key in keys:
        assert key in strings[platform], f"{key} has no name"
        assert key in icons[platform], f"{key} has no icon"

    # The slot number is interpolated into the name, so the placeholder has to
    # be in the string the entity is named from.
    for name in (
        strings["switch"]["timer"]["name"],
        strings["sensor"]["timer_schedule"]["name"],
    ):
        assert "{slot}" in name

    for path in sorted((SRC / "translations").glob("*.json")):
        entity = json.loads(path.read_text())["entity"]
        for platform, key in keys:
            assert key in entity[platform], f"{path.name} is missing {key}"
        assert "{slot}" in entity["switch"]["timer"]["name"], path.name


stubs.run_tests(globals(), "timer entities")
