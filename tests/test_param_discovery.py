#!/usr/bin/env python3
"""Offline check that parameter discovery reaches every device on the bus.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``coordinator._run_startup``, ``coordinator._discover_params`` and
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
4. neither pseudo-address is treated as a device -- the single broadcast that
   is sent is an opener, and it pays for itself by feeding the directed round,
5. discovery is reached from ``_run_startup``, and asking a dozen devices does
   not cost a dozen multi-second waits.

Run: ``python3 tests/test_param_discovery.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

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


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load():
    """Import the real coordinator + truma protocol with externals stubbed."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod("homeassistant.helpers", __path__=[], issue_registry=types.SimpleNamespace(
        async_create_issue=lambda *a, **kw: None,
        async_delete_issue=lambda *a, **kw: None,
        IssueSeverity=types.SimpleNamespace(WARNING="warning"),
    ))
    _mod("homeassistant.helpers.storage", Store=object)
    _mod("bleak_retry_connector", BleakClientWithServiceCache=object,
         establish_connection=None)

    class _Coordinator:
        """DataUpdateCoordinator stand-in that tolerates [TrumaState]."""

        def __class_getitem__(cls, _item):
            return cls

    _mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])
    # The transport is not exercised here -- the fake client below stands in
    # for it -- but bt.py drags in HA's bluetooth component, so stub it whole.
    _mod("truma_pkg.ble", TrumaBleClient=object, device_from_bluez=None)
    _mod("truma_pkg.bt", async_panel_advertising=lambda *a: False,
         async_resolve_proxy_device=None, async_wait_until_heard=None)

    def _real(name: str, package: str = "truma_pkg", path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    # Real, in dependency order: protocol.py does `from .const import ...`.
    truma_const = _real("const", "truma_pkg.truma", SRC / "truma")
    protocol = _real("protocol", "truma_pkg.truma", SRC / "truma")
    state = _real("state", "truma_pkg.truma", SRC / "truma")
    _real("const")
    coordinator = _real("coordinator")
    return truma_const, protocol, state, coordinator


TC, PROTO, STATE, COORD = _load()


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
        self.sent: list[bytes] = []
        self._coord = coord
        self._answers = answers or {}

    async def send(self, frame: bytes) -> bool:
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

    def __init__(self) -> None:
        self._state = STATE.TrumaState()
        self._last_frame = 0.0
        self._identity = {
            "muid": "MUID",
            "uuid": "uuid",
            "username": "Home Assistant",
        }

    def async_set_updated_data(self, _data) -> None:
        pass

    _discover_params = COORD.TrumaCoordinator._discover_params
    _run_startup = COORD.TrumaCoordinator._run_startup
    _on_frame = COORD.TrumaCoordinator._on_frame


def _run(coro):
    """Run a coroutine with instant sleeps, returning the recorded delays."""
    # setattr/getattr rather than attribute syntax: COORD is built by
    # importlib, so a type checker has no idea what is on it.
    real, shim = getattr(COORD, "asyncio"), _NoSleep()
    setattr(COORD, "asyncio", shim)
    try:
        asyncio.run(coro)
    finally:
        setattr(COORD, "asyncio", real)
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
    _run(coord._discover_params(client))
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

    _run(coord._discover_params(client))
    assert UNSEEDED in _discovery_dests(client), (
        "a device that spoke to us was never asked for its parameters"
    )


def test_no_device_is_asked_twice() -> None:
    """A bus where everything answers must not double the startup cost."""
    coord = _Coord()
    # Every seeded device answers for itself, the way a populated bus would.
    client = _Client(coord, answers={a: a for a in TC.DEVICE_SEED})
    _run(coord._discover_params(client))

    dests = _discovery_dests(client)
    assert len(dests) == len(set(dests)), (
        f"{len(dests) - len(set(dests))} device(s) asked more than once"
    )


def test_pseudo_addresses_are_not_devices() -> None:
    """Neither pseudo-address is a device, whatever arrives from it."""
    coord = _Coord()
    for src in (TC.DEV_BROADCAST, TC.DEV_MSG_BROKER):
        coord._on_frame({"src": src, "dest": APP_ADDR})
    assert not coord._state.seen_devices, (
        f"pseudo-addresses recorded as devices: {coord._state.seen_devices}"
    )

    client = _Client(coord)
    _run(coord._discover_params(client))
    dests = _discovery_dests(client)

    assert TC.DEV_MSG_BROKER not in dests, "the message broker was asked"
    # The broadcast is a deliberate opener, not a device: exactly one frame,
    # and it must not be re-sent per round the way a target would be.
    assert dests.count(TC.DEV_BROADCAST) == 1
    assert dests[0] == TC.DEV_BROADCAST, "the broadcast is no longer the opener"

    # A real device on the same path still gets recorded.
    coord._on_frame({"src": SCHAUDT_BLOCK, "dest": APP_ADDR})
    assert coord._state.seen_devices == {SCHAUDT_BLOCK}


def test_broadcast_answer_reaches_an_unseeded_device() -> None:
    """The opener earns its frame only if its answers feed the next round."""
    coord = _Coord()
    client = _Client(coord, answers={TC.DEV_BROADCAST: UNSEEDED})
    _run(coord._discover_params(client))
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


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("parameter discovery: all checks OK")


if __name__ == "__main__":
    _main()
