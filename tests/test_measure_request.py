#!/usr/bin/env python3
"""Offline checks for asking the tank sensors to measure.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``coordinator._request_measurements`` and the real hold loop in
``coordinator._finish_startup`` run, and the frames they emit are parsed back
with the real ``truma.protocol``.

Why this exists (reported on a Weinsberg, issue #4): a tank sensor answers with
the level it measured when it was last asked, and nothing on the bus asks it
except the panel, when its water screen is opened. So a grey tank emptied by
hand kept reporting 25 % indefinitely -- parameter discovery included, because
25 % is honestly the last measurement taken. The sensor was never wrong; the
question was hours old.

What it pins:

1. a measurement is asked for, as a write of 1 to ``<topic>.MeasureRequest``,
2. it is asked *repeatedly* while the link is held -- one reading per session
   is the bug, not the fix,
3. it is addressed to whoever reported the topic, never to an address named in
   the source: the tanks hang off an electrical block that is 0x0405 on the
   reporter's vehicle and something else on the next one,
4. a vehicle that has never reported a tank is never asked, because every
   vehicle subscribes to these topics whether or not it has the hardware,
5. and the reply lands in the state field the sensor reads, so an emptied tank
   actually moves.

Run: ``python3 tests/test_measure_request.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# The electrical block that owns the tanks on the reporting vehicle. Named
# here only to prove that nothing in the source needs to name it -- which is
# what the second address is for: same hardware, re-paired, renumbered.
BOARD = 0x0405
RENUMBERED = 0x0407
PANEL = 0x0101
APP_ADDR = 0x0501


stubs.install_homeassistant()
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
SESSION = stubs.load("session")
COORD = stubs.load("coordinator")


class _Clock:
    """Virtual loop clock, advanced by the sleeps the code under test does.

    The hold loop is a one-second tick around a sixty-second interval, so real
    time would make this test a five-minute one.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio`` stand-in whose ``sleep`` advances the clock instead."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __getattr__(self, name):  # everything else is the real asyncio
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        # Yield, so anything else awaiting still gets a turn.
        await asyncio.sleep(0)


class _Client:
    """Records sent frames and answers a measure request like a tank does.

    The answer matters: it is what refreshes the stall watchdog, so a hold loop
    that only ever sends would drop the link at 90 s and the repeat could not
    be observed at all.
    """

    def __init__(self, coord, clock: _Clock, answers: bool = True,
                 disconnect_at: float | None = None,
                 discovery: dict[int, list[tuple[str, str, int]]] | None = None,
                 ) -> None:
        self.assigned_addr = APP_ADDR
        self.sent: list[tuple[float, dict]] = []
        self._coord = coord
        self._clock = clock
        self._answers = answers
        self._disconnect_at = disconnect_at
        # Which address answers a parameter discovery, and with what. This is
        # how a tank level first becomes known on a cold start.
        self._discovery = discovery or {}
        # The level the tank actually holds, which the sensor only learns when
        # it is asked. Starts where the stale reading was.
        self.tank_level = 25

    @property
    def connected(self) -> bool:
        return (
            self._disconnect_at is None or self._clock.now < self._disconnect_at
        )

    @property
    def transport(self) -> str:
        """Which adapter this link runs over; the coordinator records it."""
        return "local"

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        parsed = PROTO.parse_v3_frame(frame)
        self.sent.append((self._clock.now, parsed))
        cbor = parsed.get("cbor") or {}

        values = self._discovery.get(parsed["dest"])
        if values is not None and parsed.get("sub_type") == TC.MBP_PARAM_DISC:
            # A discovery reply carries the values the device holds now, which
            # for a tank is whatever it last measured -- the stale reading.
            self._coord._on_frame({
                "src": parsed["dest"],
                "control_raw": TC.CTRL_MBP,
                "sub_type": TC.MBP_PARAM_DISC_RESP,
                "cbor": {"topics": [
                    {"tn": tn, "parameters": [{"pn": pn, "v": v}]}
                    for tn, pn, v in values
                ]},
            })

        if self._answers and cbor.get("pn") == TC.MEASURE_REQUEST_PARAM:
            # The device measures and pushes the result back as an ordinary
            # info notification, from its own address.
            self._coord._on_frame({
                "src": parsed["dest"],
                "control_raw": TC.CTRL_MBP,
                "sub_type": TC.MBP_INFO,
                "cbor": {
                    "tn": cbor["tn"], "pn": "Level", "v": self.tank_level,
                },
            })
        return True


class _Coord:
    """Carries only what the methods under test touch."""

    unique_id = "Truma iNetX-FFB4D1"
    poll_interval = 0
    _client = None

    def __init__(self, clock: _Clock) -> None:
        self.hass = types.SimpleNamespace(
            loop=types.SimpleNamespace(time=clock.time)
        )
        self._bus = BUS.Bus()
        self._last_frame = 0.0
        self._stop = False
        # A session that reaches startup records which address kind carried it;
        # these fixtures dial nothing, so there is nothing to record.
        self._last_kind = None
        self._session_ok = False
        self._writes_pending = 0
        self._connected_event = asyncio.Event()
        self._identity = {
            "muid": "MUID", "uuid": "uuid", "username": "Home Assistant",
        }

    def async_set_updated_data(self, _data) -> None:
        pass

    async def _run_startup(self, _client) -> None:
        """Stand in for registration + discovery, which have their own file."""

    _request_measurements = COORD.TrumaCoordinator._request_measurements
    _finish_startup = COORD.TrumaCoordinator._finish_startup
    _on_frame = COORD.TrumaCoordinator._on_frame


class _StartupCoord(_Coord):
    """As above, but running the real startup sequence end to end."""

    _run_startup = COORD.TrumaCoordinator._run_startup


def _run(coro, clock: _Clock):
    """Run a coroutine with sleeps that advance the virtual clock."""
    shim = _FastForward(clock)
    real = {mod: getattr(mod, "asyncio") for mod in (COORD, SESSION)}
    for mod in real:
        setattr(mod, "asyncio", shim)
    try:
        asyncio.run(coro)
    finally:
        for mod, value in real.items():
            setattr(mod, "asyncio", value)


def _requests(client: _Client) -> list[tuple[float, int, str]]:
    """(when, destination, topic) for every measure request sent."""
    out = []
    for when, parsed in client.sent:
        cbor = parsed.get("cbor") or {}
        if cbor.get("pn") != TC.MEASURE_REQUEST_PARAM:
            continue
        assert parsed["control_raw"] == TC.CTRL_MBP, "not an MBP frame"
        assert parsed["sub_type"] == TC.MBP_WRITE, "a measurement is a write"
        assert parsed["src"] == APP_ADDR, "sent from the wrong address"
        assert cbor["v"] == 1, f"asked with {cbor['v']!r}, not 1"
        out.append((when, parsed["dest"], cbor["tn"]))
    return out


def _seen_tanks(coord: _Coord, src: int = BOARD, level: int = 25) -> None:
    """Report a level for both tanks, as parameter discovery would."""
    coord._bus.update("FreshWater", "Level", level, src)
    coord._bus.update("GreyWater", "Level", level, src)


def test_both_tanks_are_asked_and_addressed_to_their_owner() -> None:
    # Both addresses are the same electrical block, before and after a
    # re-pairing renumbered it. An address written into the source passes the
    # first and fails the second.
    for owner in (BOARD, RENUMBERED):
        clock = _Clock()
        coord = _Coord(clock)
        client = _Client(coord, clock)
        _seen_tanks(coord, src=owner)

        _run(coord._request_measurements(client), clock)

        asked = _requests(client)
        topics = {topic for _when, _dest, topic in asked}
        assert topics == {"FreshWater", "GreyWater"}, f"asked {topics}"
        for _when, dest, topic in asked:
            assert dest == owner, (
                f"{topic} asked at 0x{dest:04X}, not at 0x{owner:04X} "
                "which is the device that reported it"
            )


def test_a_level_nobody_claims_is_asked_of_nobody() -> None:
    """The broker is not a device, so it cannot become a destination.

    This used to fall back to the panel, on the grounds that the level itself
    proved the hardware existed. It does not prove *where* it is, and the
    panel is no more the owner of a tank than the heater was the owner of the
    roof air conditioner it was being sent cooling commands (#10). Every real
    frame carries a source address; a value that carries none is parked where
    a diagnostics download shows it and nothing acts on it.
    """
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(coord, clock)
    _seen_tanks(coord, src=TC.DEV_MSG_BROKER)

    _run(coord._request_measurements(client), clock)

    assert _requests(client) == [], "asked an address nothing is behind"
    assert "FreshWater.Level" in coord._bus.unattributed


def test_two_sensors_are_both_asked_rather_than_whoever_spoke_last() -> None:
    """A bus can carry two of anything, and the panel numbers them apart.

    The destination used to be "whichever device reported the topic last", so
    on a vehicle with two tank sensors one of them was never asked and its
    reading stayed as old as the last time somebody opened the panel's water
    screen.
    """
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(coord, clock)
    coord._bus.update("FreshWater", "Level", 25, BOARD)
    coord._bus.update("FreshWater", "Level", 75, RENUMBERED)

    _run(coord._request_measurements(client), clock)

    asked = {dest for _when, dest, topic in _requests(client)
             if topic == "FreshWater"}
    assert asked == {BOARD, RENUMBERED}, asked


def test_a_vehicle_with_no_tanks_is_never_asked() -> None:
    """Every vehicle subscribes to these topics; most have no hardware."""
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(coord, clock)

    _run(coord._request_measurements(client), clock)

    assert _requests(client) == [], "asked a bus that has never mentioned a tank"


def test_the_ask_repeats_while_the_link_is_held() -> None:
    """One reading per session is the bug this fixes, not the fix."""
    clock = _Clock()
    coord = _Coord(clock)
    # Long enough to see the interval hold rather than fire once and stop.
    client = _Client(coord, clock, disconnect_at=200.0)
    _seen_tanks(coord)

    _run(coord._finish_startup(client), clock)

    fresh = [when for when, _dest, topic in _requests(client)
             if topic == "FreshWater"]
    assert len(fresh) >= 3, f"asked {len(fresh)} time(s) in 200 s: {fresh}"
    for earlier, later in pairwise(fresh):
        gap = later - earlier
        assert 55 <= gap <= 65, f"asked {gap:.1f}s apart, not about a minute"


def test_an_emptied_tank_actually_moves() -> None:
    """End to end: the reply to our question reaches the sensor's field."""
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(coord, clock, disconnect_at=70.0)
    _seen_tanks(coord)
    assert coord._bus.device(BOARD).get("GreyWater", "Level") == 25

    # The tank is emptied by hand. Nothing tells the sensor; it still holds
    # the measurement it took when the panel last asked.
    client.tank_level = 0

    _run(coord._finish_startup(client), clock)

    assert coord._bus.device(BOARD).get("GreyWater", "Level") == 0, (
        "the tank was emptied and the sensor never noticed"
    )


def test_startup_asks_after_discovery_has_found_the_tanks() -> None:
    """Order matters, and it is the whole reason step 5 is step 5.

    Nothing is asked until a tank has been reported, and on a cold start the
    only thing that reports one is the parameter discovery in step 4. Asking
    first would find no evidence, ask nothing, and leave the session's first
    reading as stale as the bug it fixes.
    """
    clock = _Clock()
    coord = _StartupCoord(clock)
    client = _Client(coord, clock, discovery={
        BOARD: [("FreshWater", "Level", 25), ("GreyWater", "Level", 25)],
    })

    _run(coord._run_startup(client), clock)

    asked = _requests(client)
    assert len(asked) == 2, (
        f"startup asked {len(asked)} time(s); discovery found the tanks"
    )
    for _when, dest, topic in asked:
        assert dest == BOARD, f"{topic} asked at 0x{dest:04X}"

    last_discovery = max(
        i for i, (_when, parsed) in enumerate(client.sent)
        if parsed.get("sub_type") == TC.MBP_PARAM_DISC
    )
    first_ask = min(
        i for i, (_when, parsed) in enumerate(client.sent)
        if (parsed.get("cbor") or {}).get("pn") == TC.MEASURE_REQUEST_PARAM
    )
    assert first_ask > last_discovery, (
        "asked for a measurement before discovery could find the tanks"
    )


def test_poll_mode_asks_once_per_poll_and_does_not_hold_the_link() -> None:
    """A poll hangs up on purpose; the ask belongs to startup there."""
    clock = _Clock()
    coord = _Coord(clock)
    coord.poll_interval = 300
    client = _Client(coord, clock)
    _seen_tanks(coord)

    # Startup is stubbed here, so ask explicitly the way step 5 does, then let
    # the poll branch run: it must hang up on quiet rather than sit on the
    # link waiting for the next interval.
    async def _poll() -> None:
        await coord._request_measurements(client)
        await coord._finish_startup(client)

    _run(_poll(), clock)

    assert len(_requests(client)) == 2, "a poll should ask each tank once"
    assert clock.now < 60, (
        f"poll held the link for {clock.now:.0f}s waiting on the interval"
    )


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("measure request: all checks OK")


if __name__ == "__main__":
    _main()
