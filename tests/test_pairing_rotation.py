#!/usr/bin/env python3
"""Offline check for the RPA rotation in ``_ensure_bonded_proxy``.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed
so the pairing loop can be driven against fake clients.

This is the mechanism that makes a re-pair work on a **stock** ESPHome
Bluetooth proxy, with no bond-clearing button and no custom firmware. Measured
on the van twice on 2026-07-26: the panel rejects the bond on the address the
proxy still holds a stale bond for (``Pairing failed due to error: 97``) and
tears the link down immediately, then advertises a fresh RPA the proxy has no
bond for, which pairs cleanly. Total ~9s.

What it pins:

1. an address that fails to bond is banished, and the next pass gets the
   panel's other RPA,
2. bonding succeeds on that second address,
3. when every candidate has been banished the avoid set is cleared rather than
   stalling forever (a rejected address heals after a panel power-cycle),
4. an address that bonds first try is never banished.

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
    _mod("truma_pkg.bt", async_resolve_proxy_device=lambda *a, **k: None)
    _mod("truma_pkg.const", LOGGER=_Logger())
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


class _Client:
    """A fake bleak client for one address: bonds only if ``bonds`` is set."""

    def __init__(self, log: dict, *, bonds: bool):
        self._log = log
        self._bonds = bonds

    async def pair(self) -> None:
        if not self._bonds:
            raise RuntimeError(STALE_BOND)

    async def start_notify(self, _char, _cb) -> None:
        if not self._bonds:
            raise RuntimeError("insufficient authentication")

    async def stop_notify(self, _char) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _run(pairing, *, bondable: set[str], addresses: list[str], stop_after: int):
    """Drive ``_ensure_bonded_proxy`` against a panel advertising ``addresses``.

    The fake resolver mimics the real one: freshest first, skipping anything in
    the avoid set, and returning ``None`` once everything is banished (which is
    what drives the loop's clear-and-retry fallback).
    """
    log = {"avoid": [], "tried": []}

    def resolve(_hass, _name, *, avoid=()):
        banished = {a.upper() for a in avoid}
        log["avoid"].append(banished)
        if len(log["avoid"]) > stop_after:
            raise _StopTest
        for address in addresses:
            if address.upper() not in banished:
                return _Device(address)
        return None

    async def connect(_cls, device, _address, **_kw):
        log["tried"].append(device.address)
        return _Client(log, bonds=device.address in bondable)

    pairing.async_resolve_proxy_device = resolve
    pairing.establish_connection = connect
    # Skip the real back-off waits; the loop's deadline is wall-clock.
    pairing.asyncio = types.SimpleNamespace(sleep=lambda _s: asyncio.sleep(0))

    async def main():
        try:
            return await pairing._ensure_bonded_proxy(None, "Truma iNetX-FFB4D1")
        except _StopTest:
            return None

    return asyncio.run(main()), log


def main() -> None:
    pairing = _load_pairing()
    stale, fresh = "5B:02:A3:3F:D8:7F", "5E:2F:65:64:A0:74"

    # 1+2. The proxy's stale bond makes the panel reject `stale`; rotation
    # moves to `fresh`, which the proxy holds no bond for, and that pairs.
    result, log = _run(
        pairing, bondable={fresh}, addresses=[stale, fresh], stop_after=4
    )
    assert result is not None, "should bond after rotating off the rejected RPA"
    assert log["tried"] == [stale, fresh], f"unexpected order: {log['tried']}"
    assert log["avoid"][1] == {stale.upper()}, "rejected RPA should be banished"

    # 3. Only the rejected address is advertised (the case a panel power-cycle
    # fixes): the avoid set must be cleared rather than stalling forever, so
    # the address gets retried instead of staying banished.
    result, log = _run(pairing, bondable=set(), addresses=[stale], stop_after=4)
    assert result is None
    assert log["tried"].count(stale) >= 2, "banished address must be retried"
    assert set() in log["avoid"][1:], "avoid set was never cleared"

    # 4. A first-try bond leaves the address alone.
    result, log = _run(
        pairing, bondable={stale}, addresses=[stale, fresh], stop_after=4
    )
    assert result is not None
    assert log["tried"] == [stale], f"should not have rotated: {log['tried']}"

    print("ok: proxy pairing rotates off a rejected RPA and bonds on the next")


if __name__ == "__main__":
    main()
