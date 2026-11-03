#!/usr/bin/env python3
"""Offline checks for the keep-alive that stands in front of the stall watchdog.

Why this exists: a panel pushes only what changes. On an iNet X with an Alde
Compact, heater off, nothing changed for minutes at a time, and the stall
watchdog -- written for a Combi, whose panel "pushes frames every few
seconds" -- tore down a healthy link every 90 s, all evening: about 90 s
connected, then 40 s reconnecting, over and over.

So a link quiet for ``_KEEPALIVE_AFTER`` seconds is asked a small question
(``session.keep_alive``), and the transport acknowledging it counts as a
frame. A link that cannot acknowledge is still dropped by the watchdog.

The real ``coordinator._finish_startup`` hold loop is run against a virtual
clock, so ten quiet minutes take milliseconds.

What it pins:

1. a quiet link that acknowledges is held, not dropped at 90 s,
2. the question is a parameter discovery of the panel's BLE device
   management (0x0601), sent as a probe, and not sent more than once per
   ``_KEEPALIVE_AFTER``,
3. a link that cannot acknowledge is still dropped by the watchdog,
4. a panel that talks on its own is never asked.

Run: ``python3 tests/test_keep_alive.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

APP_ADDR = 0x0501
PANEL = 0x0101

stubs.install_homeassistant()
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
SESSION = stubs.load("session")
COORD = stubs.load("coordinator")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio`` stand-in whose ``sleep`` advances the clock instead."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        await asyncio.sleep(0)


class _Client:
    """A panel with nothing to say, which may or may not acknowledge."""

    assigned_addr = APP_ADDR
    transport = "local"

    def __init__(self, coord, clock: _Clock, *, acks: bool = True,
                 chatty: bool = False, until: float = 600.0) -> None:
        self._coord = coord
        self._clock = clock
        self._acks = acks
        self._chatty = chatty
        self._until = until
        self.sent: list[tuple[float, dict]] = []

    @property
    def connected(self) -> bool:
        if self._chatty:
            # Something on the bus changes every ten seconds.
            if int(self._clock.now) % 10 == 0:
                self._coord._last_frame = self._clock.now
        return self._clock.now < self._until

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        parsed = PROTO.parse_v3_frame(frame)
        parsed["probe"] = probe
        self.sent.append((self._clock.now, parsed))
        return self._acks


class _Coord:
    """Carries only what the hold loop touches."""

    unique_id = "Truma iNetX-AC4C1E"
    poll_interval = 0
    _client = None

    def __init__(self, clock: _Clock) -> None:
        self.hass = types.SimpleNamespace(
            loop=types.SimpleNamespace(time=clock.time)
        )
        self._bus = BUS.Bus()
        self._last_frame = 0.0
        self._stop = False
        self._last_kind = None
        self._session_ok = False
        self._writes_pending = 0
        self._connected_event = asyncio.Event()

    def async_set_updated_data(self, _data) -> None:
        pass

    def async_sync_device_names(self) -> None:
        pass

    async def _run_startup(self, _client) -> None:
        pass

    async def _request_measurements(self, _client) -> None:
        pass

    _finish_startup = COORD.TrumaCoordinator._finish_startup


def _hold(client_kwargs: dict) -> tuple[float, _Client]:
    """Run the hold loop; return when it let go, and the client."""
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(coord, clock, **client_kwargs)
    shim = _FastForward(clock)
    real = {mod: getattr(mod, "asyncio") for mod in (COORD, SESSION)}
    for mod in real:
        setattr(mod, "asyncio", shim)
    try:
        asyncio.run(coord._finish_startup(client))
    finally:
        for mod, value in real.items():
            setattr(mod, "asyncio", value)
    return clock.now, client


def _probes(client: _Client) -> list[tuple[float, dict]]:
    return [
        (when, parsed)
        for when, parsed in client.sent
        if parsed.get("sub_type") == TC.MBP_PARAM_DISC
    ]


def test_a_quiet_link_that_answers_is_held() -> None:
    released, _ = _hold({"until": 600.0})
    assert released >= 600.0, f"a healthy quiet link was dropped at {released}s"


def test_the_question_is_small_rate_limited_and_a_probe() -> None:
    _, client = _hold({"until": 600.0})
    probes = _probes(client)
    assert probes, "nothing was asked of a quiet panel"
    for _, parsed in probes:
        assert parsed["dest"] == TC.DEV_BLE_MGMT, hex(parsed["dest"])
        assert parsed["src"] == APP_ADDR
        assert parsed["probe"] is True, "an unanswered keep-alive must not end the session"
    times = [when for when, _ in probes]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= COORD._KEEPALIVE_AFTER for gap in gaps), gaps
    # And not much more often than that either: ten minutes, about thirteen.
    assert len(probes) <= 600 / COORD._KEEPALIVE_AFTER + 1, len(probes)


def test_a_link_that_cannot_answer_is_still_dropped() -> None:
    released, client = _hold({"acks": False, "until": 600.0})
    assert _probes(client), "the watchdog fired without asking first"
    assert released <= COORD._DATA_STALL_TIMEOUT + 2, (
        f"a dead link was held for {released}s"
    )


def test_a_panel_that_talks_is_never_asked() -> None:
    released, client = _hold({"chatty": True, "until": 300.0})
    assert released >= 300.0
    assert _probes(client) == [], _probes(client)


def _main() -> None:
    stubs.run_tests(globals(), "keep-alive")


if __name__ == "__main__":
    _main()
