#!/usr/bin/env python3
"""Offline checks for the startup sequence: registration, then discovery.

No hardware, no Home Assistant install: the HA imports are stubbed so the
real ``session.run_startup``, ``session.discover_params`` and
``coordinator._on_frame`` run, and the frames they emit are parsed back with
the real ``truma.protocol``.

Why this exists (reported on a Combi 6 E + iNet X Pro, issue #7): startup asked
only the heater and the panel for their current values. Every other device on
the bus -- the electrical block, a roof air conditioner, two gas-bottle level
sensors -- publishes only when its value *changes*, so after a restart it said
nothing and its parameters stayed empty, while the panel's own screen showed
gas at 51 %, fresh water at 25 % and grey water at 0 %. Addressing each device
directly filled them in: the electrical block alone went from 4 to 31
parameters, including ``FreshWater.Level`` and ``GreyWater.Level``.

What it pins:

1. every seeded device is asked, not just the heater and the panel, and the
   frames are well-formed parameter-discovery requests from our own address,
2. a device that speaks but is in no seed is asked anyway -- addresses are
   renumbered on re-pairing, so the seed can never be authoritative,
3. no address is asked twice, or a bus that answers doubles every startup,
4. neither pseudo-address is treated as a device, and neither are we -- the
   single broadcast that is sent is an opener, and it pays for itself by
   feeding the directed round,
5. discovery is reached from ``_run_startup``, and asking a dozen devices does
   not cost a dozen multi-second waits,
6. a link that connects but never answers registration is dropped at once
   rather than carrying on into a startup that cannot work,
7. and a discovery that not one address acknowledges is dropped too -- the
   same failure one step later, and the one the registration gate cannot see.

Run: ``python3 tests/test_param_discovery.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# Addresses measured on the reporter's vehicle. They are what the seed has to
# cover; the point of naming them here is that a seed narrowed back to the
# heater and the panel fails this file loudly.
SCHAUDT_BLOCK = 0x0405
ROOF_AIRCON = 0x0406
GAS_LEFT = 0x0603
GAS_RIGHT = 0x0604
# Deliberately outside every seeded class, standing in for hardware nobody has
# reported yet.
UNSEEDED = 0x0801

APP_ADDR = 0x0501


stubs.install_homeassistant()
# The transport is not exercised here -- the fake client below stands in for
# it -- but bt.py drags in HA's bluetooth component, so stub it whole.
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
SESSION = stubs.load("session")
COORD = stubs.load("coordinator")


class _NoSleep:
    """``asyncio`` stand-in whose ``sleep`` returns at once, but records.

    The recording is the point: startup used to wait three seconds per device
    asked, which is affordable for two devices and not for twenty.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __getattr__(self, name):  # everything else is the real asyncio
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self.slept.append(delay)


class _Client:
    """Records sent frames; optionally answers as devices on the bus.

    ``answers`` maps "a frame we send to this address" to "this address then
    speaks to us", which is how a real bus behaves: a device that receives a
    discovery request replies, and the reply carries its own source address.
    """

    def __init__(self, coord, answers: dict[int, int] | None = None) -> None:
        self.assigned_addr = APP_ADDR
        # A link that carries nothing is still connected as far as the stack
        # is concerned; only the transport giving up clears this.
        self.connected = True
        self.sent: list[bytes] = []
        self._coord = coord
        self._answers = answers or {}

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        self.sent.append(frame)
        parsed = PROTO.parse_v3_frame(frame)
        speaker = self._answers.get(parsed["dest"])
        if speaker is not None:
            self._coord._on_frame({"src": speaker, "dest": APP_ADDR})
        # The real transport returns whether the panel took the frame, which
        # discovery counts to report what an unpopulated address costs.
        return True


class _Coord:
    """Carries only what the methods under test touch."""

    hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=lambda: 0.0))
    unique_id = "Truma iNetX-FFB4D1"
    # The registration response is handed the transport so it can record the
    # address the panel assigned; the fake client below is created per test.
    _client = None

    def __init__(self) -> None:
        self._bus = BUS.Bus()
        self._last_frame = 0.0
        self._identity = {
            "muid": "MUID",
            "uuid": "uuid",
            "username": "Home Assistant",
        }

    def async_set_updated_data(self, _data) -> None:
        pass

    _run_startup = COORD.TrumaCoordinator._run_startup
    _on_frame = COORD.TrumaCoordinator._on_frame
    # Startup ends by asking the on-demand sensors to measure. Nothing here
    # reports a tank, so it sends nothing -- which is the point: this file is
    # about discovery, and tests/test_measure_request.py owns that step.
    _request_measurements = COORD.TrumaCoordinator._request_measurements


def _discover(coord, client):
    """The real discovery pass, which is HA-free and takes its bus directly."""
    return SESSION.discover_params(
        client, coord._bus, coord.unique_id, coord.hass.loop.time
    )


def _run(coro):
    """Run a coroutine with instant sleeps, returning the recorded delays."""
    # setattr/getattr rather than attribute syntax: both modules are built by
    # importlib, so a type checker has no idea what is on them.
    shim = _NoSleep()
    real = {mod: getattr(mod, "asyncio") for mod in (COORD, SESSION)}
    for mod in real:
        setattr(mod, "asyncio", shim)
    try:
        asyncio.run(coro)
    finally:
        for mod, value in real.items():
            setattr(mod, "asyncio", value)
    return shim.slept


def _discovery_dests(client: _Client) -> list[int]:
    """Destination of every parameter-discovery frame the client was given."""
    dests = []
    for frame in client.sent:
        parsed = PROTO.parse_v3_frame(frame)
        if (
            parsed["control_raw"] == TC.CTRL_MBP
            and parsed.get("sub_type") == TC.MBP_PARAM_DISC
        ):
            dests.append(parsed["dest"])
    return dests


def test_every_seeded_device_is_asked() -> None:
    coord = _Coord()
    client = _Client(coord)
    _run(_discover(coord, client))
    dests = _discovery_dests(client)

    # The two that already worked must not be lost in the widening.
    assert TC.DEV_PANEL in dests, "the panel is no longer asked"
    assert TC.DEV_HEATER in dests, "the heater is no longer asked"

    for addr, what in (
        (SCHAUDT_BLOCK, "electrical block"),
        (ROOF_AIRCON, "roof air conditioner"),
        (GAS_LEFT, "left gas bottle"),
        (GAS_RIGHT, "right gas bottle"),
    ):
        assert addr in dests, f"0x{addr:04X} ({what}) is never asked"

    # Every frame must be a parameter-discovery request from our own address:
    # a discovery sent from the default app address is ignored by the broker.
    for frame in client.sent:
        parsed = PROTO.parse_v3_frame(frame)
        assert parsed["control_raw"] == TC.CTRL_MBP
        assert parsed["sub_type"] == TC.MBP_PARAM_DISC
        assert parsed["src"] == APP_ADDR
        assert parsed["dest"] in dests


def test_a_device_that_speaks_is_asked_even_when_unseeded() -> None:
    """Addresses are renumbered on re-pairing, so no fixed list can be right."""
    coord = _Coord()
    # Answering the panel's request stands in for any device speaking during
    # the first round -- a push, a subscription reply, anything at all.
    client = _Client(coord, answers={TC.DEV_PANEL: UNSEEDED})
    assert UNSEEDED not in TC.DEVICE_SEED, "pick an address the seed misses"

    _run(_discover(coord, client))
    assert UNSEEDED in _discovery_dests(client), (
        "a device that spoke to us was never asked for its parameters"
    )


def test_no_device_is_asked_twice() -> None:
    """A bus where everything answers must not double the startup cost."""
    coord = _Coord()
    # Every seeded device answers for itself, the way a populated bus would.
    client = _Client(coord, answers={a: a for a in TC.DEVICE_SEED})
    _run(_discover(coord, client))

    dests = _discovery_dests(client)
    assert len(dests) == len(set(dests)), (
        f"{len(dests) - len(set(dests))} device(s) asked more than once"
    )


def test_pseudo_addresses_are_not_devices() -> None:
    """Neither pseudo-address is a device, whatever arrives from it."""
    coord = _Coord()
    for src in (TC.DEV_BROADCAST, TC.DEV_MSG_BROKER):
        coord._on_frame({"src": src, "dest": APP_ADDR})
    assert not coord._bus.addresses, (
        f"pseudo-addresses recorded as devices: {coord._bus.addresses}"
    )

    client = _Client(coord)
    _run(_discover(coord, client))
    dests = _discovery_dests(client)

    assert TC.DEV_MSG_BROKER not in dests, "the message broker was asked"
    # The broadcast is a deliberate opener, not a device: exactly one frame,
    # and it must not be re-sent per round the way a target would be.
    assert dests.count(TC.DEV_BROADCAST) == 1
    assert dests[0] == TC.DEV_BROADCAST, "the broadcast is no longer the opener"

    # A real device on the same path still gets recorded.
    coord._on_frame({"src": SCHAUDT_BLOCK, "dest": APP_ADDR})
    assert coord._bus.addresses == {SCHAUDT_BLOCK}


def test_we_never_ask_ourselves() -> None:
    """Measured on the van: the panel puts our own address in src.

    It is answered like any other address, so nothing complains -- the only
    symptom is one wasted frame per connect and a device in the log that is
    not a device.
    """
    coord = _Coord()
    coord._bus.assigned_addr = APP_ADDR
    # A frame arriving from our own assigned address, exactly as measured.
    coord._on_frame({"src": APP_ADDR, "dest": APP_ADDR})
    assert APP_ADDR not in coord._bus.addresses, "recorded ourselves"

    # ...and even if one slipped into the set before registration completed,
    # it must not survive as far as a discovery frame.
    coord._bus.note_seen(APP_ADDR)
    client = _Client(coord)
    _run(_discover(coord, client))
    assert APP_ADDR not in _discovery_dests(client), "asked ourselves"


def test_broadcast_answer_reaches_an_unseeded_device() -> None:
    """The opener earns its frame only if its answers feed the next round."""
    coord = _Coord()
    client = _Client(coord, answers={TC.DEV_BROADCAST: UNSEEDED})
    _run(_discover(coord, client))
    assert UNSEEDED in _discovery_dests(client), (
        "a device that answered the broadcast was never asked directly"
    )


def test_startup_runs_discovery_without_paying_per_device() -> None:
    coord = _Coord()
    client = _Client(coord)
    client.assigned_addr = APP_ADDR  # already registered: skip the wait loop

    before = len(client.sent)
    slept = _run(coord._run_startup(client))
    assert len(client.sent) > before

    dests = _discovery_dests(client)
    assert set(TC.DEVICE_SEED) <= set(dests), "startup skipped the seeded devices"

    # The old code waited 3 s after each device it asked. At two devices that
    # was 6 s; applied to the seed it would be a minute of dead time on every
    # single connect, which is exactly the startup cost issue #7 complains
    # about. Waiting per *round* instead keeps it flat.
    per_device = max(len(dests), 1)
    discovery_wait = sum(slept[-per_device:])
    assert discovery_wait < 3 * per_device / 2, (
        f"discovery waited {discovery_wait}s for {per_device} devices; "
        "the wait is scaling with the bus again"
    )


def test_a_link_that_carries_nothing_is_dropped_at_once() -> None:
    """Measured on the van, 2026-09-07 22:09.

    The link came up, notifications subscribed, and BlueZ then lost the ATT
    channel: every write failed with "Service Discovery has not been performed
    yet". The panel never answered registration, and startup carried on
    regardless -- subscribing, sending identity and spending the whole
    parameter-discovery seed on a dead link, while the entities sat blank and
    "connected" until the 90 s stall watchdog finally noticed.
    """
    coord = _Coord()
    client = _Client(coord)
    # The panel answers registration by assigning an address. Leaving it at
    # the default is exactly what a link carrying nothing looks like.
    client.assigned_addr = TC.DEV_APP_DEFAULT

    raised = None
    try:
        _run(coord._run_startup(client))
    except Exception as exc:  # noqa: BLE001 - the type is HA's, stubbed here
        raised = exc
    assert raised is not None, "startup completed on a link that carries nothing"
    assert "no address" in str(raised)

    # It must give up before spending the seed: those frames are the cost this
    # exists to avoid, and the link needs handing back so the adapter's
    # connection slot is freed for the next attempt.
    assert _discovery_dests(client) == [], "discovery ran on a dead link"


def test_registration_gives_up_when_the_transport_ends_the_session() -> None:
    """An invalidated transport must not be waited out for the full timeout.

    ``TrumaBleClient.send`` ends the session when a transfer goes unanswered,
    because Ready and DataAck carry no transfer identity and a late one would
    otherwise be handed to the packet behind it. Once that has happened the
    panel cannot assign us an address over the link, so sitting out the rest
    of the registration timeout only holds the adapter's connection slot.
    """
    coord = _Coord()
    client = _Client(coord)
    client.assigned_addr = TC.DEV_APP_DEFAULT
    client.connected = False

    raised = None
    try:
        _run(coord._run_startup(client))
    except Exception as exc:  # noqa: BLE001 - the type is HA's, stubbed here
        raised = exc
    assert raised is not None, "startup carried on over a link the transport dropped"
    assert "link ended during registration" in str(raised)
    assert _discovery_dests(client) == [], "discovery ran on a dropped link"


def test_a_registered_link_still_runs_startup() -> None:
    """The gate must not fire on a panel that answered."""
    coord = _Coord()
    client = _Client(coord)
    assert client.assigned_addr != TC.DEV_APP_DEFAULT
    _run(coord._run_startup(client))
    assert set(TC.DEVICE_SEED) <= set(_discovery_dests(client))


class _SilentClient(_Client):
    """A link the panel has stopped taking frames from.

    ``TrumaBleClient.send(probe=True)`` reports whether the panel acknowledged
    the transport, and reports it by *returning False* rather than raising --
    so from discovery's side a link that has stopped carrying traffic looks
    exactly like a bus where nobody is home. ``acks`` is the set of addresses
    that still answer, so a test can say which of the two it means.
    """

    def __init__(self, coord, acks: set[int] | None = None) -> None:
        super().__init__(coord)
        self._acks = acks or set()

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        await super().send(frame, probe=probe)
        return PROTO.parse_v3_frame(frame)["dest"] in self._acks


def test_a_discovery_nothing_acknowledges_is_dropped() -> None:
    """Measured on the van, 2026-09-15 11:57.

    The link dropped while discovery was running. All 18 addresses went
    unacknowledged -- and startup ran to completion anyway, logging "connected
    and subscribed", resetting the reconnect backoff and persisting the
    address kind, on a session that had learned nothing. The registration gate
    above cannot catch this: registration had already succeeded.
    """
    coord = _Coord()
    client = _SilentClient(coord)
    assert client.assigned_addr != TC.DEV_APP_DEFAULT, "registration must pass"

    raised = None
    try:
        _run(coord._run_startup(client))
    except Exception as exc:  # noqa: BLE001 - the type is HA's, stubbed here
        raised = exc
    assert raised is not None, "startup completed on a discovery nothing answered"
    assert "no device acknowledged" in str(raised)


def test_one_acknowledgement_is_enough() -> None:
    """A partial answer is the normal case, not a failure.

    Most of the seed is addresses with nothing behind them -- no roof air
    conditioner, no gas sensors -- and those go unacknowledged on every
    healthy vehicle. Only *nothing at all* means the transport is gone, and
    the panel is always there to answer.
    """
    coord = _Coord()
    client = _SilentClient(coord, acks={TC.DEV_PANEL})
    _run(coord._run_startup(client))
    assert TC.DEV_HEATER in _discovery_dests(client), "discovery stopped early"


def _main() -> None:
    stubs.run_tests(globals(), "parameter discovery")


if __name__ == "__main__":
    _main()
