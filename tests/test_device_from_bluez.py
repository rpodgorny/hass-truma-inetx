#!/usr/bin/env python3
"""Offline check for building a BLEDevice out of BlueZ's own object.

Why it exists: a bonded panel that something already holds a link to does not
advertise -- it has a central -- so HA's discovery cache runs empty exactly when
the link is healthiest and the coordinator has nothing to hand to bleak. BlueZ
still has the device object, and its path plus properties is all bleak's BlueZ
backend needs.

This is deliberately the *only* BlueZ-specific thing left in the transport: the
connect path is plain establish_connection, which works the same over BlueZ and
over an ESPHome proxy. On a proxy this helper simply finds nothing and the
caller carries on.

Run: ``python3 tests/test_device_from_bluez.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load():
    class _BLEDevice:
        def __init__(self, address, name, details) -> None:
            self.address, self.name, self.details = address, name, details

    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=_BLEDevice)
    _mod("bleak.backends.characteristic", BleakGATTCharacteristic=object)
    _mod("bleak_retry_connector",
         BleakClientWithServiceCache=object,
         establish_connection=None)
    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", **dict.fromkeys(
        ("CHAR_CMD", "CHAR_DATA_R", "CHAR_DATA_W", "DEV_APP_DEFAULT",
         "TRANSPORT_ACK", "TRANSPORT_CONFIRM", "TRANSPORT_MSG_ACK",
         "TRANSPORT_INIT", "TRANSPORT_READY", "SERVICE_UUID"), 0))
    _mod("truma_pkg.truma.protocol", **dict.fromkeys(
        ("build_identity_frames", "build_register_frame", "build_subscribe_frame",
         "build_v3_frame", "build_write_frame", "parse_v3_frame"), None))
    _mod("truma_pkg.truma.state", TrumaState=object)

    spec = importlib.util.spec_from_file_location("truma_pkg.ble", SRC / "ble.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["truma_pkg.ble"] = module
    spec.loader.exec_module(module)
    return module


BLE = _load()

PATH = "/org/bluez/hci0/dev_50_98_93_FF_B4_D1"
PROPS = {"Address": "50:98:93:FF:B4:D1", "Alias": "Truma iNetX-FFB4D1"}


def _install_manager(properties: dict) -> None:
    manager = types.SimpleNamespace(_properties=properties)

    async def _get():
        return manager

    _mod("bleak.backends.bluezdbus", __path__=[])
    _mod("bleak.backends.bluezdbus.defs", DEVICE_INTERFACE="org.bluez.Device1")
    _mod("bleak.backends.bluezdbus.manager", get_global_bluez_manager=_get)


def test_device_is_built_from_bluez_without_an_advert() -> None:
    _install_manager({
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {"Address": "AA:BB:CC:DD:EE:FF"}},
        PATH: {"org.bluez.Device1": PROPS},
    })

    device = asyncio.run(BLE.device_from_bluez("50:98:93:ff:b4:d1"))
    assert device is not None
    assert device.address == "50:98:93:FF:B4:D1"
    # exactly what bleak's own scanner puts in details
    assert device.details == {"path": PATH, "props": PROPS}


def test_an_unknown_address_is_not_invented() -> None:
    _install_manager({PATH: {"org.bluez.Device1": PROPS}})
    assert asyncio.run(BLE.device_from_bluez("11:22:33:44:55:66")) is None


def test_no_bluez_at_all_is_not_an_error() -> None:
    """On a proxy-only host there is no BlueZ manager; that is not a failure."""
    async def _boom():
        raise RuntimeError("no D-Bus here")

    _mod("bleak.backends.bluezdbus", __path__=[])
    _mod("bleak.backends.bluezdbus.defs", DEVICE_INTERFACE="org.bluez.Device1")
    _mod("bleak.backends.bluezdbus.manager", get_global_bluez_manager=_boom)

    assert asyncio.run(BLE.device_from_bluez("50:98:93:FF:B4:D1")) is None


if __name__ == "__main__":
    test_device_is_built_from_bluez_without_an_advert()
    test_an_unknown_address_is_not_invented()
    test_no_bluez_at_all_is_not_an_error()
    print("device_from_bluez: all checks OK")
