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
    _mod("bleak.backends.device", BLEDevice=object)
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
setattr(BLE, "_BUSY_RETRY_DELAY", 0)  # no real waiting in a test


class _Device:
    address = "50:98:93:FF:B4:D1"


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


def test_busy_retries_and_never_cleans_up() -> None:
    """A busy refusal must be waited out, not cleaned up after."""
    _fresh()
    busy = RuntimeError("[org.bluez.Error.Failed] Operation already in progress")
    _Client.script = [busy, busy, None]

    client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
    client._subscribe = lambda: asyncio.sleep(0)  # type: ignore[assignment]
    asyncio.run(client.connect(_Device()))

    assert len(ATTEMPTS) == 3, ATTEMPTS
    # The cleanup would abort BlueZ's in-flight connect -- the one whose link we
    # are waiting to inherit.
    assert CLEANUPS == [], CLEANUPS


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


if __name__ == "__main__":
    test_busy_is_classified()
    test_busy_retries_and_never_cleans_up()
    test_real_failure_cleans_up_and_raises()
    print("busy retry: all checks OK")
