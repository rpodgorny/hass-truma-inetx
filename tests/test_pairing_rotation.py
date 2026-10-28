#!/usr/bin/env python3
"""Offline check for the RPA rotation in ``ensure_bonded``.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed
so the pairing loop can be driven against fake clients.

This is the mechanism that makes a re-pair work on a **stock** ESPHome
Bluetooth proxy, with no bond-clearing button and no custom firmware. Measured
on the van twice on 2026-07-26: the panel rejects the bond on the address the
proxy still holds a stale bond for (``Pairing failed due to error: 97``) and
tears the link down immediately, then advertises a fresh RPA the proxy has no
bond for, which pairs cleanly. Total ~9s.

What it pins:

1. an address that fails to bond is demoted, and the next pass gets the
   panel's other RPA,
2. bonding succeeds on that second address,
3. when the demoted address is the only one advertised it is retried rather
   than the loop stalling forever (a rejected address heals after a panel
   power-cycle),
4. an address that bonds first try is never demoted,
5. **a refusal costs the address, not the attempt** — the panel rejecting the
   bond ends that address at once instead of the retry loop spending the
   budget on it (measured 2026-10-28: two 30 s proxy timeouts were the whole
   `_PAIR_TIMEOUT`, so the rotation never ran and the owner had to press
   Submit twice), and
6. a link whose `pair()` simply hangs cannot hold the whole budget either: a
   retry that cannot fit is not started, so the rotation still gets its turn.

Run: ``python3 scripts/test_pairing_rotation.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

# The exact text the panel produced live when its device list had been cleared
# while the proxy still held its half of the bond.
STALE_BOND = "Pairing failed due to error: 97"


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
    _mod("dbus_fast", __path__=[], BusType=object, Variant=object)
    _mod("dbus_fast.aio", MessageBus=object)
    _mod(
        "dbus_fast.service",
        ServiceInterface=object,
        method=lambda *a, **k: (lambda f: f),
    )

    # A stand-in package so pairing.py's relative imports resolve without
    # executing the integration's real __init__ (which needs Home Assistant).
    _mod("truma_pkg", __path__=[str(SRC)])
    _mod(
        "truma_pkg.bt",
        async_resolve_device=lambda *a, **k: None,
        # This file is about the RPA rotation, which is the proxy-carried
        # case; a proxyless host bonds before it dials (see
        # test_pairing_local_fallback).
        async_has_proxy_route=lambda *a, **k: True,
    )
    # pairing asks the connected client which transport it got. The real check
    # imports bleak; the tests override this per run anyway.
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


class _Logger:
    """Swallow the integration's log calls."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _StopTest(Exception):
    """Raised by the fake resolver to end an otherwise-endless retry loop."""


class _Device:
    def __init__(self, address: str) -> None:
        self.address = address


class _Clock:
    """A monotonic clock the fakes can spend, because a budget needs one.

    ``ensure_bonded`` is bounded by wall-clock, so a harness whose fake work is
    instantaneous cannot see where a budget goes: every retry is free and the
    loop always reaches the rotation. Measured 2026-10-28 on the van, it does
    not — see ``PROXY_PAIR_TIMEOUT``.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# What the ESPHome proxy waits for a pairing response before giving up. Not our
# constant: it is the transport's, and on 2026-10-28 it was the whole story.
# Measured on the van (13:52:35 → 13:53:38, build 0.9.1b1): the first pair() on
# the proxy link answered "Pairing failed due to error: 97" in 134 ms, and the
# two calls after it on the SAME link each burned this 30 s — 60 s together,
# which is the entire _PAIR_TIMEOUT. ensure_bonded then had no iteration left
# to rotate to the address the panel had moved to, so the flow reported failure
# and the owner had to press Submit a second time.
PROXY_PAIR_TIMEOUT = 30.0
# What one connect costs, measured the same day (13:53:48.855 → 13:53:50.192).
DIAL = 1.2
_PAIR_TIMEOUT_TEXT = (
    "Timeout waiting for BluetoothDeviceConnectionResponse, "
    "BluetoothDevicePairingResponse after 30.0s"
)


class _Client:
    """A fake bleak client for one address: bonds only if ``bonds`` is set.

    ``slow`` models the transport's own wait: an unanswered ``pair()`` costs the
    caller ``PROXY_PAIR_TIMEOUT``. ``refuse_first`` is the shape measured on the
    van — the panel rejects the bond at once and the calls *after* it on that
    link are the ones that hang.
    """

    def __init__(
        self,
        clock: _Clock,
        *,
        bonds: bool,
        slow: bool = False,
        refuse_first: bool = False,
    ):
        self._clock = clock
        self._bonds = bonds
        self._slow = slow
        self._refuse_first = refuse_first
        self.pair_calls = 0

    async def pair(self) -> None:
        self.pair_calls += 1
        if self._bonds:
            return
        if self._refuse_first and self.pair_calls == 1:
            raise RuntimeError(STALE_BOND)
        if self._slow:
            self._clock.advance(PROXY_PAIR_TIMEOUT)
            raise RuntimeError(_PAIR_TIMEOUT_TEXT)
        raise RuntimeError(STALE_BOND)

    async def start_notify(self, _char, _cb) -> None:
        if not self._bonds:
            raise RuntimeError("insufficient authentication")

    async def stop_notify(self, _char) -> None:
        pass

    async def disconnect(self) -> None:
        pass


async def _sleep(clock: _Clock, seconds: float) -> None:
    """A back-off that costs the budget what it says it does."""
    clock.advance(seconds)
    await asyncio.sleep(0)


def _run(
    pairing,
    *,
    bondable: set[str],
    addresses: list[str],
    stop_after: int | None = None,
    slow: bool = False,
    refuse_first: bool = False,
    budget: float = 60.0,
):
    """Drive ``ensure_bonded`` against a panel advertising ``addresses``.

    The fake resolver mimics the real one: freshest first, but anything in the
    avoid set sinks below everything else rather than being withheld, so the
    last remaining address comes back even after it has failed.

    Time is the fake clock's, not the wall's: dials cost ``DIAL``, a slow
    ``pair()`` costs the proxy's timeout, and a back-off costs what it sleeps.
    Without that, a budget case cannot distinguish "rotated" from "ran out".
    """
    log = {"avoid": [], "tried": [], "clients": []}
    clock = _Clock()

    def resolve(_hass, _name, *, avoid=()):
        demoted = {a.upper() for a in avoid}
        log["avoid"].append(demoted)
        if stop_after is not None and len(log["avoid"]) > stop_after:
            raise _StopTest
        ranked = sorted(addresses, key=lambda a: a.upper() in demoted)
        return _Device(ranked[0]) if ranked else None

    async def connect(_cls, device, _address, **_kw):
        clock.advance(DIAL)
        log["tried"].append(device.address)
        client = _Client(
            clock,
            bonds=device.address in bondable,
            slow=slow,
            refuse_first=refuse_first,
        )
        log["clients"].append(client)
        return client

    pairing.async_resolve_device = resolve
    pairing.establish_connection = connect
    # Every link here is a proxy link: this file is about the address
    # rotation, and the transport branch has its own test file.
    pairing.client_is_proxy = lambda _client: True
    # Back-off sleeps are wall-clock to the code and budget to the test.
    pairing.asyncio = types.SimpleNamespace(sleep=lambda seconds: _sleep(clock, seconds))
    pairing.time = clock

    async def main():
        try:
            _bonded, client = await pairing.ensure_bonded(
                None, "Truma iNetX-FFB4D1", "50:98:B8:FF:B4:D1", timeout=budget
            )
            return client
        except _StopTest:
            return None

    return asyncio.run(main()), log, clock


def main() -> None:
    pairing = _load_pairing()
    stale, fresh = "5B:02:A3:3F:D8:7F", "5E:2F:65:64:A0:74"

    # 1+2. The proxy's stale bond makes the panel reject `stale`; rotation
    # moves to `fresh`, which the proxy holds no bond for, and that pairs.
    result, log, _clock = _run(
        pairing, bondable={fresh}, addresses=[stale, fresh], stop_after=4
    )
    assert result is not None, "should bond after rotating off the rejected RPA"
    assert log["tried"] == [stale, fresh], f"unexpected order: {log['tried']}"
    assert log["avoid"][1] == {stale.upper()}, "rejected RPA should be demoted"

    # 3. Only the rejected address is advertised (the case a panel power-cycle
    # fixes): it must be retried rather than the loop stalling forever, since
    # a demotion is not a ban and the panel may have healed.
    result, log, _clock = _run(pairing, bondable=set(), addresses=[stale], stop_after=4)
    assert result is None
    assert log["tried"].count(stale) >= 2, "demoted address must be retried"

    # 4. A first-try bond leaves the address alone.
    result, log, _clock = _run(
        pairing, bondable={stale}, addresses=[stale, fresh], stop_after=4
    )
    assert result is not None
    assert log["tried"] == [stale], f"should not have rotated: {log['tried']}"

    # 5. A refusal costs the address, not the attempt. Measured on the van
    # 2026-10-28 13:52 (0.9.1b1, owner: "I had to press submit twice"): the
    # panel rejected the bond 134 ms after the link came up ("Pairing failed
    # due to error: 97"), and the two calls after it on the SAME link each
    # waited out the proxy's 30 s. Two of those are the whole 60 s budget, so
    # `ensure_bonded` had no iteration left to rotate onto the address the
    # panel had moved to — and the cure this module documents for that error
    # exactly is the rotation.
    result, log, clock = _run(
        pairing,
        bondable={fresh},
        addresses=[stale, fresh],
        refuse_first=True,
        slow=True,
    )
    assert result is not None, (
        "a refused address must not consume the budget: the rotation is the cure "
        f"for this error, and it was given {clock.now - 1000.0:.0f}s"
    )
    assert log["tried"] == [stale, fresh], f"unexpected order: {log['tried']}"
    assert clock.now - 1000.0 < 15.0, (
        f"rotated only after {clock.now - 1000.0:.0f}s on the refused address"
    )

    # 6. A link whose pair() hangs, with no refusal to read, must not hold the
    # whole budget either: a retry that cannot fit is not started, so the one
    # address on air goes back to the rotation instead of being re-asked here.
    result, log, _clock = _run(pairing, bondable=set(), addresses=[stale], slow=True)
    assert result is None
    assert len(log["tried"]) >= 2, (
        "a link that will not bond held the whole budget: "
        f"{len(log['tried'])} dial(s)"
    )
    asked = sum(client.pair_calls for client in log["clients"])
    assert asked <= 2, f"a dead link was asked {asked} times"

    print("ok: proxy pairing rotates off a rejected RPA and bonds on the next")


if __name__ == "__main__":
    main()
