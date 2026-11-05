#!/usr/bin/env python3
"""Offline check: the forget-bond consent also reaches an ESPHome proxy (#52).

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed
so ``ensure_bonded`` and ``_forget_on_proxies`` can be driven against fakes.

The failure this pins, measured on a van 2026-11-05: the panel's device list
had been cleared while an ESPHome proxy still held its half of the bond. With
the panel's IRK still on file, the proxy resolved every RPA the panel
advertised and reported the panel only under the address it had bonded with,
so the rotation never saw anything else, and every connect to that address
timed out. The box "forget this host's existing bond" reached BlueZ only, so
~10 re-pairs failed the same way for 1.5 h; erasing the proxy's NVS brought
the real RPAs back and the next press bonded.

What it pins:

1. with consent, a connect failure through a proxy drops the proxy's bond,
   after which the panel's real address is dialled and bonds,
2. without consent nothing is dropped,
3. the consent is spent once, however many dials fail afterwards,
4. ``_forget_on_proxies`` asks the right proxy's API client for every address
   that proxy reports, counts only a real removal as success, and survives a
   proxy without an API client and an unpair call that raises.

Run: ``python3 tests/test_pairing_proxy_forget.py``
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pairing_rotation import _Clock, _Device, _StopTest, _load_pairing  # noqa: E402

PSEUDO = "7B:6C:66:19:1E:0A"  # what the proxy reported while it held the IRK
REAL = "5E:74:1D:35:15:EC"  # what the panel was actually advertising
PROXY = "94:A9:90:E3:DF:32"
CONNECT_TIMEOUT = (
    "Failed to connect after 1 attempt(s): Timeout waiting for connect "
    "response while connecting to 7B:6C:66:19:1E:0A after 20.0s"
)


class _BondingClient:
    """A proxy link that bonds: what the real address gives once unbonded."""

    async def pair(self) -> None:
        return None

    async def start_notify(self, _char, _cb) -> None:
        return None

    async def stop_notify(self, _char) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _drive(pairing, *, consent: bool, forget_works: bool = True, stop_after: int = 12):
    """Run ``ensure_bonded`` against a proxy that hides the panel behind PSEUDO."""
    state = {"proxy_bonded": True, "forgets": 0, "tried": [], "resolves": 0}
    clock = _Clock()

    def resolve(_hass, _name, *, avoid=()):
        state["resolves"] += 1
        if state["resolves"] > stop_after:
            raise _StopTest
        # While the proxy holds the bond it resolves every RPA to PSEUDO.
        return _Device(PSEUDO if state["proxy_bonded"] else REAL)

    async def connect(_cls, device, _address, **_kw):
        clock.advance(1.0)
        state["tried"].append(device.address)
        if device.address == PSEUDO:
            clock.advance(20.0)
            raise RuntimeError(CONNECT_TIMEOUT)
        return _BondingClient()

    async def forget(_hass, _name):
        state["forgets"] += 1
        if forget_works:
            state["proxy_bonded"] = False
        return forget_works

    async def sleep(seconds):
        clock.advance(seconds)
        await asyncio.sleep(0)

    pairing.async_resolve_device = resolve
    pairing.establish_connection = connect
    pairing.client_is_proxy = lambda _client: True
    pairing._live_device_path = lambda *a, **k: None  # no BlueZ object: proxy host
    pairing._forget_on_proxies = forget
    pairing.asyncio = types.SimpleNamespace(sleep=sleep)
    pairing.time = clock

    async def main():
        try:
            bonded, _client = await pairing.ensure_bonded(
                None,
                "Truma iNetX-618F12",
                PSEUDO,
                timeout=120.0,
                may_forget_bond=consent,
                own_the_link=True,
            )
            return bonded
        except _StopTest:
            return None

    return asyncio.run(main()), state


class _Unpairing:
    def __init__(self, success: bool) -> None:
        self.success = success


class _Api:
    """An ESPHome API client holding bonds for ``bonded`` addresses."""

    def __init__(self, bonded: set[str], *, broken: bool = False) -> None:
        self.bonded = {int(a.replace(":", ""), 16) for a in bonded}
        self.asked: list[int] = []
        self.broken = broken

    async def bluetooth_device_unpair(self, address: int):
        self.asked.append(address)
        if self.broken:
            raise RuntimeError("API connection lost")
        if address in self.bonded:
            self.bonded.discard(address)
            return _Unpairing(True)
        return _Unpairing(False)


def _hass(entries):
    return types.SimpleNamespace(
        config_entries=types.SimpleNamespace(async_entries=lambda domain: entries)
    )


def _entry(mac: str, api):
    return types.SimpleNamespace(
        runtime_data=types.SimpleNamespace(
            device_info=types.SimpleNamespace(bluetooth_mac_address=mac),
            client=api,
        )
    )


def _check_forget_on_proxies() -> None:
    pairing = _load_pairing()
    bt = sys.modules["truma_pkg.bt"]

    # 4a. The proxy that holds the bond is asked for every address it reports,
    # and a real removal counts as success.
    api = _Api({PSEUDO})
    bt.async_panel_addresses_by_proxy = lambda _h, _n: {PROXY: {PSEUDO, REAL}}
    dropped = asyncio.run(pairing._forget_on_proxies(_hass([_entry(PROXY.lower(), api)]), "x"))
    assert dropped, "a removed bond must count as dropped"
    assert sorted(api.asked) == sorted(
        int(a.replace(":", ""), 16) for a in (PSEUDO, REAL)
    ), f"every reported address must be asked: {api.asked}"
    assert not api.bonded

    # 4b. Nothing to remove is not success.
    api = _Api(set())
    dropped = asyncio.run(pairing._forget_on_proxies(_hass([_entry(PROXY, api)]), "x"))
    assert not dropped, "an address with no bond must not count as dropped"

    # 4c. A proxy without an API client, and one whose call raises, are
    # skipped without breaking the flow.
    dropped = asyncio.run(pairing._forget_on_proxies(_hass([]), "x"))
    assert not dropped
    api = _Api({PSEUDO}, broken=True)
    dropped = asyncio.run(pairing._forget_on_proxies(_hass([_entry(PROXY, api)]), "x"))
    assert not dropped, "a failing unpair call must not count as dropped"

    # 4d. A lookup that blows up is swallowed too: this is best effort.
    def boom(_h, _n):
        raise RuntimeError("bluetooth not loaded")

    bt.async_panel_addresses_by_proxy = boom
    assert not asyncio.run(pairing._forget_on_proxies(_hass([]), "x"))


def main() -> None:
    # 1. Consent given: the stale proxy bond is dropped once, then the real
    # address is dialled and bonds.
    bonded, state = _drive(_load_pairing(), consent=True)
    assert bonded is True, f"should bond after dropping the proxy's bond: {state}"
    assert state["forgets"] == 1, f"forget must run exactly once: {state}"
    assert state["tried"][-1] == REAL, f"the real address must be dialled: {state}"

    # 2. No consent: the proxy's bond is never touched.
    bonded, state = _drive(_load_pairing(), consent=False, stop_after=6)
    assert not bonded
    assert state["forgets"] == 0, "nothing may be dropped without consent"

    # 3. Consent is spent once even when dropping did not help.
    bonded, state = _drive(_load_pairing(), consent=True, forget_works=False, stop_after=8)
    assert not bonded
    assert state["forgets"] == 1, f"consent must be spent once: {state['forgets']}"

    _check_forget_on_proxies()
    print("ok: forget-bond reaches the ESPHome proxy and unblocks the re-pair")


if __name__ == "__main__":
    main()
