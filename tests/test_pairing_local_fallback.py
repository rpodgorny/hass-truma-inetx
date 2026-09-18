#!/usr/bin/env python3
"""Offline check that a panel which refuses every unbonded link still pairs.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed
so the real ``pairing.ensure_bonded`` loop and the real
``pairing._ensure_bonded_bluez`` loop run against fakes.

Why this exists (issue #26, a Raspi 3B with a USB dongle and no proxy,
2026-09-17): the panel was heard on its identity address and nothing else, and
every connect died the same way 750 ms in --

    Failed to connect after 1 attempt(s): failed to discover services,
    device disconnected

-- twenty-six times in sixty seconds. ``ensure_bonded`` dispatches on the
client it gets, so a connect that never returns one never reaches the dispatch:
``Device1.Pair()`` was never called, the Just Works agent was never registered,
and the one procedure that could have bonded the panel sat behind the connect
that bonding was supposed to make possible.

What it pins:

1. a connect that has failed on every advertised address hands over to the
   BlueZ path instead of re-dialling,
2. every address gets its turn first, so the RPA rotation that cures a stale
   proxy bond is not cut short,
3. a *bond* failure is still the rotation's business and does not trigger it,
4. the hand-over needs a BlueZ object path to pair on, and without one nothing
   is claimed,
5. the bond is dropped only after the panel has refused it -- try, then
   remove,
6. and only when the caller could not establish a link at all, so Reconfigure
   on a working bond is reported rather than destroyed, whether the fast path
   or the loop is what reads it,
7. one removal attempt per call, so a RemoveDevice BlueZ refuses cannot eat
   the timeout,
8. a bond this host holds alone is never reported as success,
9. and the link ``Pair()`` opened is dropped before the bond is reported --
   BlueZ keeps it, and the kernel then refuses the session's every connect.

Run: ``python3 tests/test_pairing_local_fallback.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-401D00"
# The identity address from #26 -- its last three bytes are the panel's name
# suffix, which is what makes it the identity rather than a rotating RPA.
IDENTITY = "84:72:93:40:1D:00"
OTHER = "5E:2F:65:64:A0:74"
HCI0 = "/org/bluez/hci0"
DEV = f"{HCI0}/dev_84_72_93_40_1D_00"
# The RPA a panel in add-device mode advertises on, and the service UUID that
# is all it offers to recognise it by.
RPA = "46:01:42:5A:64:5F"
RPA_DEV = f"{HCI0}/dev_46_01_42_5A_64_5F"
TRUMA_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"

# The wording bleak_retry_connector produced on the reporter's host, verbatim.
NO_SERVICES = (
    "Failed to connect after 1 attempt(s): "
    "failed to discover services, device disconnected"
)


class _Logger:
    """Swallow the integration's log calls."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _V:
    """dbus_fast Variant stand-in, as read back off a property dict."""

    def __init__(self, value):
        self.value = value


class _ServiceInterface:
    """dbus_fast ServiceInterface stand-in that accepts its interface name."""

    def __init__(self, _name: str | None = None) -> None:
        pass


def _load_pairing():
    """Import ``pairing.py`` with every external dependency stubbed out."""

    def _mod(name: str, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module
        return module

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object)
    _mod(
        "bleak_retry_connector",
        BleakClientWithServiceCache=object,
        establish_connection=None,
    )
    # BusType needs the member pairing actually asks for: the rotation test
    # gets away with `object` because it never reaches the D-Bus path, and this
    # file is the one that does.
    _mod(
        "dbus_fast",
        __path__=[],
        BusType=types.SimpleNamespace(SYSTEM="system"),
        Variant=lambda *args: args,
    )
    _mod("dbus_fast.aio", MessageBus=_Bus)
    _mod(
        "dbus_fast.service",
        ServiceInterface=_ServiceInterface,
        method=lambda *a, **k: (lambda f: f),
    )

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod(
        "truma_pkg.bt",
        async_resolve_device=lambda *a, **k: None,
        # Default: no proxy in earshot, which is the #26 host. Tests that
        # need the other kind set pairing.async_has_proxy_route themselves.
        async_has_proxy_route=lambda *a, **k: False,
    )
    _mod("truma_pkg.ble", client_is_proxy=lambda _client: True)
    _mod(
        "truma_pkg.const",
        LOGGER=_Logger(),
        has_truma_uuid=lambda uuids: any(
            str(u).lower().startswith("fc31") for u in uuids
        ),
    )
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", CHAR_CMD="cmd-char")

    spec = importlib.util.spec_from_file_location(
        "truma_pkg.pairing", SRC / "pairing.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["truma_pkg.pairing"] = module
    spec.loader.exec_module(module)
    return module


# --- the BlueZ side, as far as _ensure_bonded_bluez can see -------------------


class _AlreadyExists(Exception):
    """What BlueZ answers Pair() with when it already holds a bond."""

    def __str__(self) -> str:
        return "[org.bluez.Error.AlreadyExists] Already Exists"


class _AuthFailed(Exception):
    """What BlueZ answers Pair() with when the panel refuses."""

    def __str__(self) -> str:
        return "[org.bluez.Error.AuthenticationFailed] Authentication Failed"


class _InProgress(Exception):
    """What BlueZ answers Pair() with while a pairing it took is still running."""

    def __str__(self) -> str:
        return "[org.bluez.Error.InProgress] In Progress"


class _Bluez:
    """BlueZ plus the panel behind it, recording every call it is asked for."""

    def __init__(
        self,
        *,
        paired: bool,
        accepts: bool,
        removable: bool = True,
        pairs_after: int | None = None,
    ):
        self.paired = paired
        self.accepts = accepts
        self.removable = removable
        # Polls after which a Pair() the daemon is still working on completes.
        # Until then every further call earns InProgress, as on the van.
        self.pairs_after = pairs_after
        self.polls = 0
        self.discovering = False
        self.paired_path: str | None = None
        # Whether BlueZ currently holds a link to the panel. Pair() opens one
        # and keeps it, which is what _release_link exists to undo.
        self.connected = False
        # A leftover object for the identity address with no RSSI behind it,
        # beside the RPA the panel is really on -- the van's state after a
        # bond was dropped.
        self.stale_identity = False
        self.calls: list[str] = []

    def objects(self) -> dict:
        self.polls += 1
        if self.pairs_after is not None and self.polls > self.pairs_after:
            self.paired = True
        if not self.stale_identity:
            return {
                DEV: {
                    "org.bluez.Device1": {
                        "Address": _V(IDENTITY),
                        "Name": _V(PANEL),
                        "Paired": _V(self.paired),
                        "RSSI": _V(-52),
                    }
                }
            }
        # The identity object is a leftover: matched by address, but BlueZ is
        # not seeing it, so it carries no RSSI. The live RPA is nameless, as a
        # panel in add-device mode is, and only BlueZ's own discovery creates
        # an object for it at all.
        objects = {
            DEV: {
                "org.bluez.Device1": {
                    "Address": _V(IDENTITY),
                    "Name": _V(PANEL),
                    "Paired": _V(self.paired),
                }
            }
        }
        if self.discovering:
            objects[RPA_DEV] = {
                "org.bluez.Device1": {
                    "Address": _V(RPA),
                    "Paired": _V(self.paired),
                    "RSSI": _V(-51),
                    "UUIDs": _V([TRUMA_UUID]),
                }
            }
        return objects

    async def pair(self) -> None:
        self.calls.append("pair")
        if self.pairs_after is not None and not self.paired:
            # The daemon took the first call and is still working on it.
            raise _InProgress
        if self.paired:
            # BlueZ will not pair a device it already has a key for, which is
            # exactly the state a panel that forgot its half leaves behind.
            raise _AlreadyExists
        if not self.accepts:
            raise _AuthFailed
        self.paired = True
        # Pair() bonds over a link of its own, and BlueZ keeps it afterwards.
        self.connected = True

    async def remove(self, path: str) -> None:
        self.calls.append("remove")
        assert path == DEV, f"removed the wrong object: {path}"
        if not self.removable:
            raise RuntimeError("[org.bluez.Error.Failed] Does Not Exist")
        self.paired = False


class _Bus:
    """dbus_fast MessageBus stand-in; the interfaces come from _get_interface."""

    current: "_Bluez | None" = None

    def __init__(self, **_kw) -> None:
        pass

    async def connect(self) -> "_Bus":
        return self

    def export(self, _path, _obj) -> None:
        assert _Bus.current is not None
        _Bus.current.calls.append("export_agent")

    def disconnect(self) -> None:
        assert _Bus.current is not None
        _Bus.current.calls.append("bus_disconnect")


class _ObjectManager:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_get_managed_objects(self) -> dict:
        return self._bluez.objects()


class _AgentManager:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_register_agent(self, _path, capability) -> None:
        assert capability == "NoInputNoOutput", capability
        self._bluez.calls.append("register_agent")

    async def call_request_default_agent(self, _path) -> None:
        self._bluez.calls.append("request_default_agent")

    async def call_unregister_agent(self, _path) -> None:
        self._bluez.calls.append("unregister_agent")


class _Device1:
    def __init__(self, bluez: _Bluez, path: str) -> None:
        self._bluez = bluez
        self._path = path

    async def call_pair(self) -> None:
        self._bluez.paired_path = self._path
        await self._bluez.pair()

    async def call_disconnect(self) -> None:
        self._bluez.calls.append("disconnect")
        self._bluez.connected = False


class _Properties:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_set(self, _interface, prop, _value) -> None:
        self._bluez.calls.append(f"set:{prop}")

    async def call_get(self, _interface, prop) -> _V:
        assert prop == "Connected", prop
        return _V(self._bluez.connected)


class _Adapter1:
    def __init__(self, bluez: _Bluez) -> None:
        self._bluez = bluez

    async def call_remove_device(self, path) -> None:
        await self._bluez.remove(path)

    async def call_set_discovery_filter(self, _filter) -> None:
        self._bluez.calls.append("discovery_filter")

    async def call_start_discovery(self) -> None:
        self._bluez.calls.append("start_discovery")
        self._bluez.discovering = True

    async def call_stop_discovery(self) -> None:
        self._bluez.calls.append("stop_discovery")
        self._bluez.discovering = False


def _interfaces(bluez: _Bluez):
    """A ``_get_interface`` that answers from ``bluez``, asserting the paths."""

    async def _get_interface(_bus, path, interface):
        if interface == "org.freedesktop.DBus.ObjectManager":
            assert path == "/", path
            return _ObjectManager(bluez)
        if interface == "org.bluez.AgentManager1":
            assert path == "/org/bluez", path
            return _AgentManager(bluez)
        if interface == "org.bluez.Device1":
            assert path in (DEV, RPA_DEV), path
            return _Device1(bluez, path)
        if interface == "org.freedesktop.DBus.Properties":
            return _Properties(bluez)
        if interface == "org.bluez.Adapter1":
            # The bond must be dropped on the adapter being paired, not on
            # whichever adapter happens to know the device.
            assert path == HCI0, path
            return _Adapter1(bluez)
        raise AssertionError(f"unexpected interface {interface}")

    return _get_interface


class _BluezDevice:
    """A BLEDevice as the resolver hands it back from a local adapter."""

    def __init__(self, path: str) -> None:
        self.details = {"path": path}
        self.address = IDENTITY


# --- part one: ensure_bonded hands over when no link can be made -------------


class _Device:
    def __init__(self, address: str) -> None:
        self.address = address


class _StopTest(Exception):
    """Ends a loop that is meant to run until its deadline."""


def _run_ensure_bonded(
    pairing,
    *,
    addresses: list[str],
    bluez_sees: bool,
    has_proxy: bool = True,
    connects: bool = False,
    connect_delay: float = 0.0,
    fresh_addresses: bool = False,
    bonds: bool = False,
    stop_after: int = 200,
):
    """Drive ``ensure_bonded`` and report what it tried and where it went."""
    log: dict = {"tried": [], "handover": [], "budget": [], "bonded_over_link": 0}

    def resolve(_hass, _name, *, avoid=(), local_only=False, prefer_identity=False):
        if local_only:
            # What _live_device_path asks: a local adapter's device, which is
            # the only kind carrying the BlueZ object path Pair() needs.
            return _BluezDevice(DEV) if bluez_sees else None
        if len(log["tried"]) >= stop_after:
            # Only a guard against a loop that never reaches its deadline; the
            # tests below are meant to end on the deadline, not here.
            raise _StopTest
        if fresh_addresses:
            # A panel whose RPA rotates faster than the rotation can wrap: no
            # candidate is ever offered twice, so nothing but the reserve can
            # end the dialling.
            n = len(log["tried"])
            return _Device(f"4{n % 10}:00:00:00:{n // 10:02X}:{n % 10:02X}")
        demoted = {a.upper() for a in avoid}
        ranked = sorted(addresses, key=lambda a: a.upper() in demoted)
        return _Device(ranked[0]) if ranked else None

    async def connect(_cls, device, _address, **_kw):
        log["tried"].append(device.address)
        if connect_delay:
            await asyncio.sleep(connect_delay)
        if not connects:
            raise RuntimeError(NO_SERVICES)
        return object()

    async def bond_over_link(_name, _client):
        log["bonded_over_link"] += 1
        return bonds

    async def bluez_bond(_name, _address, **kwargs):
        log["handover"].append(kwargs.get("trust_existing_bond"))
        log["budget"].append(kwargs.get("timeout"))
        return True

    # Put every one of these back afterwards. _ensure_bonded_bluez is stubbed
    # here and exercised for real in part two, and a leak between the two reads
    # as the real loop passing when it never ran.
    patches = {
        "async_resolve_device": resolve,
        "establish_connection": connect,
        "client_is_proxy": lambda _client: True,
        "_bond_over_link": bond_over_link,
        "_ensure_bonded_bluez": bluez_bond,
        "async_has_proxy_route": lambda *_a, **_k: has_proxy,
        # The loop's back-off is wall-clock seconds and its deadline is not;
        # skip most of the waiting without touching asyncio.wait_for, which the
        # D-Bus helpers still need.
        "asyncio": types.SimpleNamespace(sleep=lambda _s: asyncio.sleep(0.01)),
    }
    saved = {name: getattr(pairing, name) for name in patches}
    for name, value in patches.items():
        setattr(pairing, name, value)

    async def main():
        try:
            return await pairing.ensure_bonded(
                object(), PANEL, IDENTITY, adapter_path=HCI0, timeout=0.25
            )
        except _StopTest:
            return None, None

    try:
        return asyncio.run(main()), log
    finally:
        for name, value in saved.items():
            setattr(pairing, name, value)


def test_a_panel_that_refuses_every_link_is_bonded_through_bluez(pairing) -> None:
    """#26: the connect bonding is *for* cannot be a precondition of bonding.

    One address on air, every connect dying before services resolve. The old
    loop re-dialled it for the whole timeout and never registered an agent.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert client is None, "the BlueZ path holds no link to hand off"
    assert log["handover"] == [False], (
        f"expected one hand-over with the bond unproven: {log['handover']}"
    )
    # Dialled once, then handed over rather than spending the timeout.
    assert log["tried"] == [IDENTITY], f"kept re-dialling: {log['tried']}"


def test_every_address_is_tried_before_handing_over(pairing) -> None:
    """The rotation keeps its turn: a second address may be the live one.

    A panel fresh out of pairing advertises a phantom RPA beside a working
    one, and the phantom fails to establish. Handing over on the first failure
    would bond locally while a proxy-carried address was still untried.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY, OTHER], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert log["tried"] == [IDENTITY, OTHER], f"rotation cut short: {log['tried']}"
    assert log["handover"] == [False]


def test_a_bond_failure_is_not_a_connect_failure(pairing) -> None:
    """Connected and then refused is the rotation's case, not the fallback's.

    This is the error-97 path: the proxy holds a bond the panel has dropped,
    the panel tears the link down on the protected write, and the cure is the
    panel's next RPA -- not a local bond on a host whose sessions run over the
    proxy, which is the mistake c91f711 was written to stop.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing,
        addresses=[IDENTITY, OTHER],
        bluez_sees=True,
        has_proxy=True,
        connects=True,
        bonds=False,
    )
    assert bonded is False
    assert log["handover"] == [], "a bond failure must not hand over to BlueZ"
    assert log["bonded_over_link"] >= 2, "the link path should have kept trying"


def test_nothing_is_claimed_without_a_bluez_path(pairing) -> None:
    """No object path, no Pair(): the failure is somewhere else.

    A proxy-only host whose connects fail transiently must not be told a local
    bond was made, because there is no local adapter to make one on.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=False, has_proxy=True
    )
    assert bonded is False
    assert client is None
    assert log["handover"] == [], "handed over with no adapter to pair on"
    assert len(log["tried"]) > 1, "should have kept retrying the connect"


def test_a_proxyless_host_bonds_before_it_dials(pairing) -> None:
    """The regression c91f711 left behind, and what 0.7.1b5 did instead.

    A panel with no bond drops every link it is offered, so on a host where no
    proxy can hear it a connect cannot succeed and only spends the budget --
    ~20 s per candidate address, of a 60 s timeout. Before connect-first this
    host went straight to BlueZ with the whole of it.
    """
    (bonded, client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY, OTHER], bluez_sees=True, has_proxy=False
    )
    assert bonded is True
    assert client is None
    assert log["tried"] == [], f"dialled a panel that cannot answer: {log['tried']}"
    assert len(log["handover"]) == 1


def test_a_host_with_a_proxy_still_dials_first(pairing) -> None:
    """The case c91f711 is actually about, and it keeps its behaviour.

    Where a proxy can hear the panel, a local bond can land on a path nothing
    connects over, and every later session then fails at encryption. So the
    link in hand still decides, and BlueZ is the fallback it always was.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing, addresses=[IDENTITY], bluez_sees=True, has_proxy=True
    )
    assert bonded is True
    assert log["tried"] == [IDENTITY], "bonded locally without dialling"


def test_the_connect_rotation_cannot_starve_the_bond(pairing) -> None:
    """The hand-over is guaranteed its reserve, however slow the dialling.

    Measured on the van (2026-09-18): two addresses, ~20 s each to give up on,
    and the hand-over inherited 15 s of a 60 s budget and timed out. The same
    bond took 3.2 s when the rotation had left it room. Here the panel offers
    a fresh address every time, so the rotation can never wrap and only the
    reserve can end it.
    """
    (bonded, _client), log = _run_ensure_bonded(
        pairing,
        addresses=[],
        bluez_sees=True,
        has_proxy=True,
        connect_delay=0.02,
        fresh_addresses=True,
    )
    assert bonded is True
    assert log["tried"], "handed over without dialling at all"
    assert len(log["handover"]) == 1, f"never handed over: {log['handover']}"
    assert log["budget"][0] > 0, f"handed over with nothing left: {log['budget']}"


# --- part two: try, then remove ----------------------------------------------


def _run_bluez_bond(
    pairing,
    bluez: _Bluez,
    *,
    trust: bool,
    timeout: float = 1.0,
    adapter_path: str | None = HCI0,
):
    """Drive the real ``_ensure_bonded_bluez`` against a fake BlueZ."""
    saved = {
        name: getattr(pairing, name)
        for name in ("_get_interface", "async_resolve_device", "_POLL_INTERVAL")
    }
    pairing._get_interface = _interfaces(bluez)
    pairing.async_resolve_device = lambda *a, **k: _BluezDevice(DEV)
    pairing._POLL_INTERVAL = 0
    _Bus.current = bluez
    try:
        return asyncio.run(
            pairing._ensure_bonded_bluez(
                PANEL,
                IDENTITY,
                adapter_path=adapter_path,
                timeout=timeout,
                hass=object(),
                trust_existing_bond=trust,
            )
        )
    finally:
        for name, value in saved.items():
            setattr(pairing, name, value)
        _Bus.current = None


def test_a_stale_bond_is_dropped_only_after_the_panel_refuses(pairing) -> None:
    """Try, then remove -- and in that order.

    BlueZ holds a key the panel discarded, so Pair() comes back AlreadyExists.
    Only then is the bond dropped, and the pass after it bonds clean.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    pairs = [c for c in bluez.calls if c in ("pair", "remove")]
    assert pairs[0] == "pair", f"removed before asking the panel: {bluez.calls}"
    assert pairs.count("remove") == 1, f"removed more than once: {bluez.calls}"
    assert pairs[-1] == "pair", "nothing re-paired after the bond was dropped"
    # The procedure the reporter's host never reached at all.
    assert "register_agent" in bluez.calls
    assert "unregister_agent" in bluez.calls


def test_the_panel_is_paired_where_bluez_can_see_it(pairing) -> None:
    """A leftover object must not outrank the address the panel is on.

    Measured on the van (2026-09-18): after the bond was dropped, BlueZ kept a
    Device1 for the identity address -- matched by address, carrying no RSSI
    because nothing was behind it -- while the panel advertised fresh RPAs.
    Pair() went to the leftover and spent the whole 60 s on it. BlueZ creates
    no object for those RPAs at all unless it is discovering, which Home
    Assistant's MGMT-socket scanner does not make it do.
    """
    bluez = _Bluez(paired=False, accepts=True)
    bluez.stale_identity = True
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "start_discovery" in bluez.calls, "never asked BlueZ to look"
    assert "stop_discovery" in bluez.calls, "left discovery running"
    assert bluez.paired_path == RPA_DEV, (
        f"paired the leftover instead of the live address: {bluez.paired_path}"
    )


def test_a_pairing_still_running_is_not_asked_again(pairing) -> None:
    """InProgress is BlueZ working, not the panel refusing.

    ``Device1.Pair()`` is abandoned at _PAIR_CALL_TIMEOUT but the daemon keeps
    going, and answers every further call with InProgress. Measured on the van
    (2026-09-18): two InProgress answers were the whole of a 15 s window, and
    the bond that completed underneath them was never noticed. The loop has to
    watch Paired instead of re-issuing the call.
    """
    bluez = _Bluez(paired=False, accepts=True, pairs_after=3)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert bluez.calls.count("pair") == 1, (
        f"re-issued a call BlueZ was still running: {bluez.calls}"
    )
    assert "remove" not in bluez.calls, "InProgress was read as a refusal"


def test_a_proven_bond_is_taken_from_the_loop_too(pairing) -> None:
    """Trust is about the caller's evidence, not about which check saw it.

    Without an adapter to scope to, ``_already_bonded`` refuses to answer --
    it cannot tell this panel's bond from one on a disabled dongle -- so the
    loop is where a proven bond gets read. It must be read the same way there:
    the caller established a link before handing over, so the bond works, and
    re-pairing it would put a panel that is not in add-device mode at risk for
    nothing.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True, adapter_path=None) is True
    assert "remove" not in bluez.calls, f"destroyed a working bond: {bluez.calls}"
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"


def test_a_working_bond_is_reported_not_destroyed(pairing) -> None:
    """Reconfigure on a healthy bond must stay cheap and non-destructive.

    The caller got as far as a link before handing over, so a bond on this
    adapter is one the panel honours. Removing it up front would be the
    cheaper code and would leave a panel that is not in add-device mode with
    no bond at all.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "remove" not in bluez.calls, f"destroyed a working bond: {bluez.calls}"
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"


def test_an_unpaired_panel_is_not_removed_first(pairing) -> None:
    """Nothing to drop: the host holds no bond, so this is a plain pairing."""
    bluez = _Bluez(paired=False, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=False) is True
    assert "remove" not in bluez.calls, (
        f"removed a bond that was not there: {bluez.calls}"
    )
    assert bluez.calls.count("pair") == 1


def test_a_refused_removal_is_attempted_once_and_claims_nothing(pairing) -> None:
    """A RemoveDevice BlueZ will not do must not eat the timeout, or lie.

    The bond stays suspect, so the call runs out rather than reporting a
    success it cannot see -- and it does not spend the remaining seconds
    retrying a D-Bus call that has already said no.
    """
    bluez = _Bluez(paired=True, accepts=True, removable=False)
    assert _run_bluez_bond(pairing, bluez, trust=False) is False
    assert bluez.calls.count("remove") == 1, f"retried the removal: {bluez.calls}"
    assert bluez.calls.count("pair") > 1, "should have kept asking the panel"


def test_the_link_the_bond_was_made_over_is_dropped(pairing) -> None:
    """The bond is not the end of it: what pairing opened has to be closed.

    ``Device1.Pair()`` bonds over a link of its own and BlueZ keeps it, so the
    session that follows finds the panel already connected -- and the kernel
    refuses a second link to the same peer, instantly, every time. Measured on
    the van (2026-09-18): bonded at 11:24:42, then 32 connects failed with
    ``[org.bluez.Error.Failed] Input/output error`` while that link sat idle
    for fifteen minutes. One device, one entity, disconnected.
    """
    bluez = _Bluez(paired=False, accepts=True)
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "disconnect" in bluez.calls, f"left the pairing link up: {bluez.calls}"
    assert bluez.connected is False
    assert bluez.calls.index("pair") < bluez.calls.index("disconnect"), (
        f"disconnected before pairing: {bluez.calls}"
    )


def test_a_bond_that_was_already_there_leaves_no_link_either(pairing) -> None:
    """The fast path is where a *retry* lands, which is where this bit.

    A pairing attempt that left a link behind makes the next attempt cheap --
    BlueZ still reports Paired, so the loop is never entered -- and the link is
    still there afterwards. That is the second failed pairing in a row the
    reporter sees, with nothing in the log to say why.
    """
    bluez = _Bluez(paired=True, accepts=True)
    bluez.connected = True
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "pair" not in bluez.calls, f"re-paired needlessly: {bluez.calls}"
    assert "disconnect" in bluez.calls, f"left the link up: {bluez.calls}"
    assert bluez.connected is False


def test_nothing_is_disconnected_when_no_link_is_up(pairing) -> None:
    """A bond reported with no link of its own is left entirely alone.

    Over a proxy the coordinator adopts the live client, and a Disconnect()
    fired on the way past would drop the session the caller is about to hand
    over. So the property is read first and the call only follows a link that
    is actually there.
    """
    bluez = _Bluez(paired=True, accepts=True)
    assert bluez.connected is False
    assert _run_bluez_bond(pairing, bluez, trust=True) is True
    assert "disconnect" not in bluez.calls, (
        f"disconnected a link nobody had: {bluez.calls}"
    )


def main() -> None:
    pairing = _load_pairing()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(pairing)
            print(f"ok  {name}")
    print("all passed")


try:  # pytest drives the same checks through a fixture
    import pytest
except ImportError:  # pragma: no cover
    pass
else:

    @pytest.fixture
    def pairing():
        return _load_pairing()


if __name__ == "__main__":
    main()
