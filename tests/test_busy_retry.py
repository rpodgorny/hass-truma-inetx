#!/usr/bin/env python3
"""Offline check for the "BlueZ is already connecting" retry.

Why it exists: on a host without controller address resolution, bluetoothd
reconnects the bonded panel by itself in the background. When that attempt is
in flight, our own connect is refused with
``org.bluez.Error.Failed: Operation already in progress``. Treating that as a
session failure was measured to be actively wrong on the van: BlueZ's attempt
completes seconds later and leaves a fully resolved link, while our next try
only came after the backoff and collided again. So a busy error must mean
"look again shortly", and it must NOT trigger the stale-connection cleanup --
that would abort the attempt we want to inherit.

Run: ``python3 tests/test_busy_retry.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

HANG = object()
CLEANUPS: list[str] = []
ATTEMPTS: list[str] = []


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Client:
    """Fails with whatever the script says, then connects."""

    script: list[BaseException | None] = []

    def __init__(self, device, disconnected_callback=None, timeout=None) -> None:
        del disconnected_callback, timeout  # accepted, not exercised here
        self.address: str = getattr(device, "address", str(device))
        self.is_connected = False

    async def connect(self) -> None:
        ATTEMPTS.append(self.address)
        outcome = _Client.script.pop(0)
        if outcome is HANG:
            await asyncio.Event().wait()  # the D-Bus reply that never comes
        if outcome is not None:
            raise outcome
        self.is_connected = True


async def _close_stale(address: str) -> None:
    CLEANUPS.append(address)


def _load():
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object)
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    class _BLEDevice:
        def __init__(self, address, name, details) -> None:
            self.address, self.name, self.details = address, name, details

    _mod("bleak.backends.device", BLEDevice=_BLEDevice)
    _mod("bleak.backends.characteristic", BleakGATTCharacteristic=object)
    _mod(
        "bleak_retry_connector",
        BleakClientWithServiceCache=_Client,
        close_stale_connections_by_address=_close_stale,
    )
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
setattr(BLE, "_WATCH_INTERVAL", 0.01)  # no real waiting in a test


class _Device:
    address = "50:98:93:FF:B4:D1"
    details = {"path": "/org/bluez/hci0/dev_50_98_93_FF_B4_D1"}


def _fresh() -> None:
    CLEANUPS.clear()
    ATTEMPTS.clear()


def test_busy_is_classified() -> None:
    assert BLE.is_retryable_error(RuntimeError("[org.bluez.Error.Failed] Operation already in progress"))
    # A bare Error.Failed is the one that matters: measured on the van, BlueZ
    # answers the caller with it and completes the connection anyway.
    assert BLE.is_retryable_error(RuntimeError("[org.bluez.Error.Failed] Software caused connection abort"))
    assert not BLE.is_retryable_error(TimeoutError())
    # A bond problem must not be retried -- each attempt costs a bond slot.
    assert not BLE.is_retryable_error(RuntimeError("[org.bluez.Error.AuthenticationFailed] blah"))


def test_busy_waits_for_bluez_and_never_cleans_up() -> None:
    """A busy refusal means watch, not dial again, and never clean up.

    Re-dialing while BlueZ's attempt is in flight collects another refusal and
    is how the link ends up established with no owner; the cleanup would abort
    that attempt outright.
    """
    _fresh()
    busy = RuntimeError("[org.bluez.Error.Failed] Operation already in progress")
    _Client.script = [busy, None]
    checked: list[bool] = []

    async def _holds(_device) -> bool:
        checked.append(True)
        return True  # BlueZ's own attempt landed while we watched

    original = BLE._bluez_holds_link
    setattr(BLE, "_bluez_holds_link", _holds)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        client._subscribe = lambda: asyncio.sleep(0)  # type: ignore[assignment]
        asyncio.run(client.connect(_Device()))
    finally:
        setattr(BLE, "_bluez_holds_link", original)

    assert checked, "the busy path must watch BlueZ for the link"
    assert len(ATTEMPTS) == 2, ATTEMPTS
    assert CLEANUPS == [], CLEANUPS


def test_busy_that_never_lands_gives_up_and_cleans() -> None:
    _fresh()
    busy = RuntimeError("[org.bluez.Error.Failed] Operation already in progress")
    _Client.script = [busy]

    async def _never(_device) -> bool:
        return False

    original = BLE._bluez_holds_link
    setattr(BLE, "_bluez_holds_link", _never)
    setattr(BLE, "_WATCH_SECONDS", 0.05)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        try:
            asyncio.run(client.connect(_Device()))
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a link that never lands must propagate")
    finally:
        setattr(BLE, "_bluez_holds_link", original)

    assert len(ATTEMPTS) == 1, ATTEMPTS
    assert CLEANUPS == ["50:98:93:FF:B4:D1"], CLEANUPS


def test_real_failure_cleans_up_and_raises() -> None:
    _fresh()
    _Client.script = [TimeoutError("no answer")]

    client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
    try:
        asyncio.run(client.connect(_Device()))
    except TimeoutError:
        pass
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a real connect failure must propagate")

    assert len(ATTEMPTS) == 1, ATTEMPTS
    assert CLEANUPS == ["50:98:93:FF:B4:D1"], CLEANUPS


def test_device_is_built_from_bluez_without_an_advert() -> None:
    """A panel BlueZ already holds does not advertise; find it anyway.

    This is the state the panel spends most of its life in once bonded, and it
    is exactly when HA's discovery cache is empty -- so the address, the object
    path and the properties have to come from BlueZ itself.
    """
    path = "/org/bluez/hci0/dev_50_98_93_FF_B4_D1"
    props = {"Address": "50:98:93:FF:B4:D1", "Alias": "Truma iNetX-FFB4D1"}

    manager = types.SimpleNamespace(_properties={
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {"Address": "AA:BB:CC:DD:EE:FF"}},
        path: {"org.bluez.Device1": props},
    })

    async def _manager():
        return manager

    _mod("bleak.backends.bluezdbus", __path__=[])
    _mod("bleak.backends.bluezdbus.defs", DEVICE_INTERFACE="org.bluez.Device1")
    _mod("bleak.backends.bluezdbus.manager", get_global_bluez_manager=_manager)

    device = asyncio.run(BLE.device_from_bluez("50:98:93:ff:b4:d1"))
    assert device is not None
    assert device.address == "50:98:93:FF:B4:D1"
    assert device.details == {"path": path, "props": props}

    assert asyncio.run(BLE.device_from_bluez("11:22:33:44:55:66")) is None


def test_unanswered_connect_is_abandoned_once_bluez_has_the_link() -> None:
    """bluetoothd never replies to a Connect it did not itself complete.

    When the link arrives on the accept path, ``att_connect_cb()`` never runs,
    so ``device->connect`` is never answered and the call hangs forever. Waiting
    longer cannot help; the only way through is to stop waiting for the reply
    and attach to the link BlueZ is already holding.
    """
    _fresh()
    _Client.script = [HANG, None]
    seen = 0

    async def _ready(_device):
        nonlocal seen
        seen += 1
        return seen > 1  # not up on the first look, up on the second

    original = BLE._bluez_link_ready
    setattr(BLE, "_bluez_link_ready", _ready)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        device = _Device()
        # _connect_or_adopt, not connect(): the subscribe that follows it needs
        # a real GATT client and is not what this pins.
        asyncio.run(client._connect_or_adopt(_Client(device), device))
    finally:
        setattr(BLE, "_bluez_link_ready", original)

    # One hung dial, then one attach -- and no stale-connection cleanup, which
    # would have torn down the very link we just adopted.
    assert len(ATTEMPTS) == 2, ATTEMPTS
    assert CLEANUPS == [], CLEANUPS


if __name__ == "__main__":
    test_busy_is_classified()
    test_busy_waits_for_bluez_and_never_cleans_up()
    test_busy_that_never_lands_gives_up_and_cleans()
    test_real_failure_cleans_up_and_raises()
    test_device_is_built_from_bluez_without_an_advert()
    test_unanswered_connect_is_abandoned_once_bluez_has_the_link()
    print("busy retry: all checks OK")
