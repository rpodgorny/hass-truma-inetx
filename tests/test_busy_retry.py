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
DIALS: list[bool] = []
CALLS: list[str] = []
ATTEMPTS: list[str] = []


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Client:
    """A client only ever ATTACHES now; the dial is a bare D-Bus call."""

    def __init__(self, device, disconnected_callback=None, timeout=None) -> None:
        del disconnected_callback, timeout  # accepted, not exercised here
        self.address: str = getattr(device, "address", str(device))
        self.is_connected = False

    async def connect(self) -> None:
        ATTEMPTS.append(self.address)
        self.is_connected = True


async def _record_call(_ble_device, member: str) -> None:
    CALLS.append(member)


def _scripted_dial(script: list) -> object:
    """Stand in for _dbus_connect, handing out one scripted outcome per call."""

    async def _dial(_ble_device) -> None:
        DIALS.append(True)
        outcome = script.pop(0)
        if outcome is HANG:
            await asyncio.Event().wait()  # the D-Bus reply that never comes
        if outcome is not None:
            raise outcome

    return _dial


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
    _mod("bleak.exc", BleakError=type("BleakError", (Exception,), {}))
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
setattr(BLE, "_BLUEZ_GRACE", 0.0)      # dial straight away
setattr(BLE, "_DIAL_SETTLE", 0.02)     # no real settle wait in a test


class _Device:
    address = "50:98:93:FF:B4:D1"
    details = {"path": "/org/bluez/hci0/dev_50_98_93_FF_B4_D1"}


def _fresh() -> None:
    CLEANUPS.clear()
    ATTEMPTS.clear()
    DIALS.clear()
    CALLS.clear()


def test_busy_is_classified() -> None:
    assert BLE.is_retryable_error(RuntimeError("[org.bluez.Error.Failed] Operation already in progress"))
    # A bare Error.Failed is the one that matters: measured on the van, BlueZ
    # answers the caller with it and completes the connection anyway.
    assert BLE.is_retryable_error(RuntimeError("[org.bluez.Error.Failed] Software caused connection abort"))
    assert not BLE.is_retryable_error(TimeoutError())
    # A bond problem must not be retried -- each attempt costs a bond slot.
    assert not BLE.is_retryable_error(RuntimeError("[org.bluez.Error.AuthenticationFailed] blah"))


def test_busy_retries_and_never_cleans_up() -> None:
    """A busy refusal means ask again shortly, and never clean up.

    Purely watching was measured to be worse than retrying: when the refusal
    came from a wedged ATT bearer, bt-ghostbuster cleared it a minute later and
    nothing was there to dial, so the whole window was spent idle. The cleanup
    must stay out of it either way -- it would abort the attempt we want.
    """
    _fresh()
    busy = RuntimeError("[org.bluez.Error.Failed] Operation already in progress")
    async def _ready_once_dialled_twice(_device) -> bool:
        # Nothing to attach to until the second dial gets through, so the retry
        # has to be a real dial rather than a lucky attach.
        return len(DIALS) >= 2

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    setattr(BLE, "_bluez_link_ready", _ready_once_dialled_twice)
    setattr(BLE, "_dbus_connect", _scripted_dial([busy, None]))
    setattr(BLE, "_BUSY_RETRY_INTERVAL", 0.01)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        client._subscribe = lambda: asyncio.sleep(0)  # type: ignore[assignment]
        asyncio.run(client.connect(_Device()))
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)

    assert len(DIALS) == 2, DIALS       # refused once, then let in
    assert len(ATTEMPTS) == 1, ATTEMPTS  # one client, built to attach
    assert CLEANUPS == [], CLEANUPS


def test_busy_that_never_clears_gives_up_and_cleans() -> None:
    _fresh()
    busy = RuntimeError("[org.bluez.Error.Failed] Operation already in progress")
    async def _not_ready(_device) -> bool:
        return False

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    setattr(BLE, "_bluez_link_ready", _not_ready)
    setattr(BLE, "_dbus_connect", _scripted_dial([busy] * 40))
    setattr(BLE, "_BUSY_RETRY_INTERVAL", 0.01)
    setattr(BLE, "_WATCH_SECONDS", 0.05)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        try:
            asyncio.run(client.connect(_Device()))
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("a refusal that never clears must propagate")
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)
        setattr(BLE, "_WATCH_SECONDS", 150.0)

    assert CLEANUPS == ["50:98:93:FF:B4:D1"], CLEANUPS


def test_real_failure_cleans_up_and_raises() -> None:
    _fresh()
    original_dial = BLE._dbus_connect
    setattr(BLE, "_dbus_connect", _scripted_dial([TimeoutError("no answer")]))
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        try:
            asyncio.run(client.connect(_Device()))
        except TimeoutError:
            pass
        else:  # pragma: no cover - the point of the test
            raise AssertionError("a real connect failure must propagate")
    finally:
        setattr(BLE, "_dbus_connect", original_dial)

    assert len(DIALS) == 1, DIALS
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


def test_unanswered_dial_is_cleared_not_abandoned() -> None:
    """An unanswered dial must be torn down at BlueZ's end, not walked away from.

    Leaving it pending holds bluetoothd's att_io open, which pins an hci_conn in
    BT_CONNECT, which makes passive scanning accept-list filtered -- the adapter
    goes blind to every other device until HA power-cycles it. Only
    Device1.Disconnect() closes that socket, so the session is given up and
    retried rather than adopted.
    """
    _fresh()
    seen = 0

    async def _ready(_device):
        nonlocal seen
        seen += 1
        return seen > 1  # not up on the first look, up on the second

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    original_call = BLE._device_call
    setattr(BLE, "_bluez_link_ready", _ready)
    setattr(BLE, "_dbus_connect", _scripted_dial([HANG]))
    setattr(BLE, "_device_call", _record_call)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        device = _Device()
        try:
            asyncio.run(
                client._connect_or_adopt(device, lambda: _Client(device))
            )
        except BLE.ClearedDial:
            pass
        else:  # pragma: no cover - the point of the test
            raise AssertionError("clearing a dial must give up the session")
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)
        setattr(BLE, "_device_call", original_call)

    assert len(DIALS) == 1, DIALS
    assert CALLS == ["Disconnect"], CALLS   # cleared at BlueZ's end
    assert ATTEMPTS == [], ATTEMPTS         # and NOT adopted
    assert CLEANUPS == [], CLEANUPS


def test_a_cleared_dial_is_retryable() -> None:
    """The link exists, so the caller must come straight back for it."""
    assert BLE.is_retryable_error(BLE.ClearedDial("cleared"))


def test_a_link_bluez_already_holds_is_attached_not_dialled() -> None:
    """Dialling a device BlueZ already holds only creates an unanswered call."""
    _fresh()

    async def _ready(_device):
        return True

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    setattr(BLE, "_bluez_link_ready", _ready)
    setattr(BLE, "_dbus_connect", _scripted_dial([]))
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        device = _Device()
        asyncio.run(client._connect_or_adopt(device, lambda: _Client(device)))
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)

    assert DIALS == [], DIALS                 # nothing was dialled
    assert len(ATTEMPTS) == 1, ATTEMPTS       # the attach, and nothing else


def test_no_dial_while_a_clear_is_still_settling() -> None:
    """After clearing, wait for BlueZ instead of undoing the clear.

    Clearing drops BlueZ's Connected but leaves the kernel link, which
    bt-ghostbuster only takes down at its 2-minute mark. Dialling inside that
    window is how the van oscillated -- dial, BlueZ arrives, clear, dial again
    -- recreating the pending connect that blinds the adapter every time.
    """
    _fresh()

    async def _never_ready(_device) -> bool:
        return False

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    setattr(BLE, "_bluez_link_ready", _never_ready)
    setattr(BLE, "_dbus_connect", _scripted_dial([None] * 40))
    setattr(BLE, "_CONNECT_TIMEOUT", 0.05)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        client._cleared_at = 1e18  # as if we had just cleared one
        device = _Device()
        try:
            asyncio.run(client._connect_or_adopt(device, lambda: _Client(device)))
        except TimeoutError:
            pass
        else:  # pragma: no cover - the point of the test
            raise AssertionError("nothing was connectable, so it must time out")
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)
        setattr(BLE, "_CONNECT_TIMEOUT", 180.0)

    assert DIALS == [], DIALS  # the quiet period held


def test_a_dial_that_lands_is_not_mistaken_for_an_unanswerable_one() -> None:
    """The link appearing while we dial usually means OUR dial just landed.

    BlueZ takes ~21 s to complete a dial and the device properties go true a
    moment before the D-Bus reply arrives. Treating that instant as "BlueZ got
    there first" made us Disconnect our own successful connection; the bearer
    BlueZ then disowned was dropped by bt-ghostbuster and the session timed out
    having thrown away a working link. Four times in a row on the van.
    """
    _fresh()
    ready = False

    async def _ready(_device) -> bool:
        return ready

    async def _dial_that_lands(_ble_device) -> None:
        nonlocal ready
        DIALS.append(True)
        ready = True          # properties flip first...
        await asyncio.sleep(0.01)  # ...reply arrives a moment later
        return None

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    original_call = BLE._device_call
    setattr(BLE, "_bluez_link_ready", _ready)
    setattr(BLE, "_dbus_connect", _dial_that_lands)
    setattr(BLE, "_device_call", _record_call)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        device = _Device()
        asyncio.run(client._connect_or_adopt(device, lambda: _Client(device)))
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)
        setattr(BLE, "_device_call", original_call)

    assert len(DIALS) == 1, DIALS
    assert CALLS == [], CALLS           # nothing was cleared...
    assert len(ATTEMPTS) == 1, ATTEMPTS  # ...and the link was used


def test_a_proxy_device_bypasses_all_the_bluez_machinery() -> None:
    """A panel reached over an ESPHome proxy has no D-Bus object anywhere.

    Everything in the BlueZ path -- watching Connected/ServicesResolved, dialling
    over the bus, clearing a dial with Disconnect -- needs an object path. A
    proxy device has none, so it must go straight to bleak instead of waiting out
    _BLUEZ_GRACE and then failing on a path that was never going to exist.
    """
    _fresh()

    class _ProxyDevice:
        address = "50:98:93:FF:B4:D1"
        details = {"source": "esp32-proxy", "address_type": "public"}

    async def _boom(_device):  # must never be consulted
        raise AssertionError("BlueZ machinery must not run for a proxy device")

    original = BLE._bluez_link_ready
    original_dial = BLE._dbus_connect
    setattr(BLE, "_bluez_link_ready", _boom)
    setattr(BLE, "_dbus_connect", _boom)
    try:
        client = BLE.TrumaBleClient({"muid": "m", "uuid": "u", "username": "n"})
        device = _ProxyDevice()
        asyncio.run(client._connect_or_adopt(device, lambda: _Client(device)))
    finally:
        setattr(BLE, "_bluez_link_ready", original)
        setattr(BLE, "_dbus_connect", original_dial)

    assert DIALS == [], DIALS            # no D-Bus dial
    assert len(ATTEMPTS) == 1, ATTEMPTS  # one plain bleak connect
    assert CLEANUPS == [], CLEANUPS


if __name__ == "__main__":
    test_busy_is_classified()
    test_busy_retries_and_never_cleans_up()
    test_busy_that_never_clears_gives_up_and_cleans()
    test_real_failure_cleans_up_and_raises()
    test_device_is_built_from_bluez_without_an_advert()
    test_unanswered_dial_is_cleared_not_abandoned()
    test_a_cleared_dial_is_retryable()
    test_a_dial_that_lands_is_not_mistaken_for_an_unanswerable_one()
    test_no_dial_while_a_clear_is_still_settling()
    test_a_proxy_device_bypasses_all_the_bluez_machinery()
    test_a_link_bluez_already_holds_is_attached_not_dialled()
    print("busy retry: all checks OK")
