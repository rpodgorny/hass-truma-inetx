#!/usr/bin/env python3
"""Offline check that bonding dispatches to the transport it actually has.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed so
the real ``bt.async_resolve_proxy_device`` and the real
``pairing.ensure_bonded`` dispatch run.

Why this exists (observed on the van, 2026-08-23): with the USB dongle disabled
and only the Pi's built-in adapter enabled, pairing failed every time with

    proxy pair(): [org.bluez.Error.AuthenticationFailed] Authentication Failed

and bluetoothd said ``No agent available for request type 2``. The resolver is
named ``async_resolve_proxy_device`` but deliberately falls back to a *local*
adapter when no proxy can hear the panel, so using it as a proxy-presence test
made the proxy branch win on every ESP-less host. That branch registers no BlueZ
pairing agent -- only ``_ensure_bonded_bluez`` does -- so the local path was
unreachable and Just Works confirmation could never be answered.

What it pins:

1. ``remote_only=True`` returns ``None`` when only a local adapter hears the
   panel, while the default still returns that local device (the coordinator
   depends on the fallback, so it must not change),
2. ``remote_only=True`` still returns the proxy device when one can hear it,
3. ``ensure_bonded`` takes the local BlueZ path -- the one with the agent -- when
   there is no proxy route,
4. and still takes the proxy path when there is one.

Run: ``python3 tests/test_pairing_transport_dispatch.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-FFB4D1"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"
RPA = "5E:ED:DC:5F:D5:A3"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Info:
    def __init__(self, address: str = RPA, time: float = 1.0) -> None:
        self.name = PANEL
        self.service_uuids = [SERVICE_UUID]
        self.address = address
        self.time = time
        self.rssi = -45
        self.connectable = True


class _RemoteScanner:
    """Stands in for habluetooth.BaseHaRemoteScanner (an ESP32 proxy)."""


class _LocalScanner:
    """Stands in for a scanner backed by a host adapter (hci0/hci1)."""


class _ScannerDevice:
    def __init__(self, address: str, remote: bool) -> None:
        self.scanner = _RemoteScanner() if remote else _LocalScanner()
        self.advertisement = _Info(address=address)
        self.ble_device = f"{'proxy' if remote else 'local'}:{address}"


ADVERTS: list[_Info] = []
SCANNERS: dict[str, list[_ScannerDevice]] = {}


def _load():
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.components", __path__=[])
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=object)
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
    _mod(
        "homeassistant.components.bluetooth",
        async_discovered_service_info=lambda _hass, connectable=True: list(ADVERTS),
        async_scanner_devices_by_address=lambda _hass, address, connectable=True: list(
            SCANNERS.get(address, ())
        ),
    )
    # Without this, is_remote_scanner() hits ImportError and calls every scanner
    # local -- which would make the proxy cases below pass for the wrong reason.
    _mod("habluetooth", BaseHaRemoteScanner=_RemoteScanner)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.const", LOGGER=_Logger())
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", SERVICE_UUID=SERVICE_UUID, CHAR_CMD="cmd-char")

    def _real(name: str):
        spec = importlib.util.spec_from_file_location(
            f"truma_pkg.{name}", SRC / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_pkg.{name}"] = module
        spec.loader.exec_module(module)
        return module

    bt = _real("bt")
    pairing = _real("pairing")
    return bt, pairing


BT, PAIRING = _load()


def _only(*, remote: bool) -> None:
    """Let exactly one scanner kind hear the panel."""
    ADVERTS[:] = [_Info()]
    SCANNERS.clear()
    SCANNERS[RPA] = [_ScannerDevice(RPA, remote=remote)]


def test_remote_only_ignores_a_local_adapter() -> None:
    _only(remote=False)
    # The default keeps the fallback: the coordinator relies on it to reach the
    # panel at all on an ESP-less host.
    assert BT.async_resolve_proxy_device(None, PANEL) == f"local:{RPA}"
    # The narrow question answers honestly.
    assert BT.async_resolve_proxy_device(None, PANEL, remote_only=True) is None


def test_remote_only_still_finds_a_proxy() -> None:
    _only(remote=True)
    assert BT.async_resolve_proxy_device(None, PANEL, remote_only=True) == f"proxy:{RPA}"


def _dispatch(*, remote: bool) -> str:
    """Run ensure_bonded() far enough to see which bonding path it chose."""
    _only(remote=remote)
    chosen: list[str] = []

    async def _proxy(_hass, _name, *, timeout=60.0):
        chosen.append("proxy")
        return None

    async def _bluez(_name, _address, *, adapter_path=None, timeout=60.0):
        chosen.append("bluez")
        return True

    PAIRING._ensure_bonded_proxy = _proxy
    PAIRING._ensure_bonded_bluez = _bluez
    asyncio.run(PAIRING.ensure_bonded(None, PANEL, RPA, timeout=2.0))
    return chosen[0] if chosen else "none"


def test_local_only_uses_the_bluez_path_that_registers_an_agent() -> None:
    # The regression: this returned "proxy", so no agent was ever registered and
    # BlueZ answered AuthenticationFailed.
    assert _dispatch(remote=False) == "bluez"


def test_proxy_present_still_uses_the_proxy_path() -> None:
    assert _dispatch(remote=True) == "proxy"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
