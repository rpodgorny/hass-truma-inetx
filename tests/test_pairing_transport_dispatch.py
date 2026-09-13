#!/usr/bin/env python3
"""Offline check that bonding dispatches to the transport it actually has.

No hardware, no Home Assistant install: the HA/bleak/dbus imports are stubbed so
the real ``bt.async_resolve_device`` and the real
``pairing.ensure_bonded`` dispatch run.

Why this exists (observed on the van, 2026-08-23): with the USB dongle disabled
and only the Pi's built-in adapter enabled, pairing failed every time with

    proxy pair(): [org.bluez.Error.AuthenticationFailed] Authentication Failed

and bluetoothd said ``No agent available for request type 2``. Pairing chose
its procedure by asking which kind of scanner could *hear* the panel, and the
resolver of the day fell back to a local adapter, so the proxy branch won on
every ESP-less host. That branch registers no BlueZ pairing agent -- only
``_ensure_bonded_bluez`` does -- so Just Works confirmation could never be
answered.

The fix went further than adding a filter: which scanner heard an advert never
predicted the transport, because Home Assistant scores the connection paths
itself at every connect. ``ensure_bonded`` now connects first and dispatches on
the client it actually got.

What it pins:

1. the bonding path follows the connected client, not the scanner that heard
   the panel -- both ways round,
2. the bleak link is dropped before BlueZ is asked to pair on the same panel,
3. ``local_only`` -- the one transport filter left -- answers only with a local
   adapter's device, because only that carries a BlueZ object path.

Then the failure that fix uncovered: with the local path finally reached, pairing
reported success in 18 ms and the panel never saw it. The bond search was
unscoped, so it matched the USB dongle's surviving bond -- HA's entry for that
adapter was disabled, but the adapter was still powered and still bonded. So:

5. an existing bond is not trusted when the pairing adapter is unknown,
6. it is trusted when found on the adapter being paired,
7. and scoping to an adapter without the bond finds nothing.

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


class _BLEDevice(str):
    """A BLEDevice stand-in that still compares as ``"proxy:<mac>"``.

    The resolver checks in this file assert on that string; ``ensure_bonded``
    dials ``device.address``. Being both keeps one fixture serving both.
    """

    address: str

    def __new__(cls, source: str, address: str):
        device = super().__new__(cls, f"{source}:{address}")
        device.address = address
        return device


class _ScannerDevice:
    def __init__(self, address: str, remote: bool) -> None:
        self.scanner = _RemoteScanner() if remote else _LocalScanner()
        self.advertisement = _Info(address=address)
        self.ble_device = _BLEDevice("proxy" if remote else "local", address)


# Whatever owned sys.modules['habluetooth'] before this file loaded, if anything.
_HABLUETOOTH_BEFORE: object | None = None

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
    global _HABLUETOOTH_BEFORE
    _HABLUETOOTH_BEFORE = sys.modules.get("habluetooth")
    _mod("habluetooth", BaseHaRemoteScanner=_RemoteScanner)

    _mod("truma_pkg", __path__=[str(SRC)])
    # pairing asks the connected client which transport HA gave it. The real
    # check reads the client's backend module and needs bleak; every test here
    # overrides it to say what HA is pretending to have chosen.
    _mod("truma_pkg.ble", client_is_proxy=lambda _client: True)
    _mod("truma_pkg.const", LOGGER=_Logger(), LOCAL_NAME_PREFIX="Truma iNetX")
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

# bt.is_remote_scanner() resolves habluetooth at CALL time, not import time, so
# whichever test module registered the stub last decides whose _RemoteScanner
# class isinstance() is checked against. Leaving ours in sys.modules made
# test_no_route_issue.py's proxy device look local and fail. Take ours back out
# after loading, and put it in only while our own tests run.
_HABLUETOOTH_STUB = sys.modules["habluetooth"]
_restore_before = _HABLUETOOTH_BEFORE
if _restore_before is None:
    del sys.modules["habluetooth"]
else:
    sys.modules["habluetooth"] = _restore_before


def _install_stub() -> object | None:
    """Point habluetooth at our stub; return whatever was there before."""
    previous = sys.modules.get("habluetooth")
    sys.modules["habluetooth"] = _HABLUETOOTH_STUB
    return previous


def _restore_stub(previous: object | None) -> None:
    if previous is None:
        sys.modules.pop("habluetooth", None)
    else:
        sys.modules["habluetooth"] = previous


try:  # pytest is not needed for the standalone `python3 tests/...` run
    import pytest
except ImportError:  # pragma: no cover
    pass
else:

    @pytest.fixture(autouse=True)
    def _own_habluetooth_stub():
        """Scope this file's stub to this file's tests."""
        previous = _install_stub()
        try:
            yield
        finally:
            _restore_stub(previous)


def _only(*, remote: bool) -> None:
    """Let exactly one scanner kind hear the panel."""
    ADVERTS[:] = [_Info()]
    SCANNERS.clear()
    SCANNERS[RPA] = [_ScannerDevice(RPA, remote=remote)]


def test_local_only_ignores_a_proxy() -> None:
    """The one transport filter left, and the one ``_bluez_path()`` asks.

    Only a local adapter's device carries the BlueZ object path that
    ``Device1.Pair()`` is called on. A proxy's view of the same address has
    none, so answering with it would leave the local pairing loop believing
    BlueZ has never heard of a panel it can plainly see -- sixty seconds of an
    agent registered and nothing to pair with.
    """
    _only(remote=True)
    assert BT.async_resolve_device(None, PANEL) == f"proxy:{RPA}"
    assert BT.async_resolve_device(None, PANEL, local_only=True) is None

    _only(remote=False)
    assert BT.async_resolve_device(None, PANEL, local_only=True) == f"local:{RPA}"


class _Client:
    """A connected client that records whether it was dropped."""

    def __init__(self) -> None:
        self.dropped = False

    async def disconnect(self) -> None:
        self.dropped = True


def _dispatch(*, heard_by_proxy: bool, connected_via_proxy: bool) -> str:
    """Run ensure_bonded() far enough to see which bonding path it took."""
    _only(remote=heard_by_proxy)
    chosen: list[str] = []
    clients: list[_Client] = []

    async def _connect(_cls, _device, _address, **_kw):
        client = _Client()
        clients.append(client)
        return client

    async def _bluez(_name, _address, *, adapter_path=None, timeout=60.0, hass=None):
        chosen.append("bluez")
        return True

    async def _link(_name, _client):
        chosen.append("link")
        return True

    PAIRING.establish_connection = _connect
    PAIRING.client_is_proxy = lambda _client: connected_via_proxy
    PAIRING._ensure_bonded_bluez = _bluez
    PAIRING._bond_over_link = _link
    asyncio.run(PAIRING.ensure_bonded(None, PANEL, RPA, timeout=2.0))
    if chosen == ["bluez"]:
        assert clients and clients[0].dropped, (
            "the bleak link must be dropped before BlueZ is asked to pair"
        )
    return chosen[0] if chosen else "none"


def test_dispatch_follows_the_client_not_the_scanner() -> None:
    """Bond the way the link in hand needs, not the way the adverts suggest.

    Home Assistant scores the connection paths itself and re-picks at every
    connect, so "a proxy can hear the panel" does not mean the connection will
    go through it. This used to probe exactly that and take the proxy path
    whenever a proxy was in earshot, which on an ESP-less host registered no
    BlueZ agent (``No agent available for request type 2``, seen on the van
    2026-08-23) and, worse, on a host with both could bond the panel to the
    proxy while every session ran over the local adapter -- a bond on a path
    nothing uses, failing at encryption forever after.
    """
    # A proxy can hear it, but HA connected us over the host's own adapter:
    # bonding is BlueZ's job, agent and all.
    assert _dispatch(heard_by_proxy=True, connected_via_proxy=False) == "bluez"
    # Nothing but a local adapter heard it, yet the link we got is a proxy
    # link: bleak pair() is then the right procedure.
    assert _dispatch(heard_by_proxy=False, connected_via_proxy=True) == "link"


# --- the stale-bond false success -----------------------------------------
#
# Observed on the van 2026-08-23: pairing reported success in 18 ms while the
# panel sat in add-device mode seeing nothing. The USB dongle's HA config entry
# was disabled, but the adapter stayed powered and still held the Truma bond, so
# the unscoped device search matched it and _is_paired() said yes.

HCI0 = "/org/bluez/hci0"
HCI1 = "/org/bluez/hci1"


class _V:
    """dbus_fast Variant stand-in."""

    def __init__(self, value):
        self.value = value


def _objects() -> dict:
    """BlueZ objects: the panel bonded on hci0, absent from hci1."""
    return {
        f"{HCI0}/dev_50_98_93_FF_B4_D1": {
            "org.bluez.Device1": {
                "Address": _V("50:98:93:FF:B4:D1"),
                "Name": _V(PANEL),
                "Paired": _V(True),
            }
        }
    }


def test_stale_bond_on_another_adapter_is_not_accepted() -> None:
    objs = _objects()
    path = PAIRING._find_device(objs, name=PANEL, address="50:98:93:FF:B4:D1")
    # Unscoped, the search still finds the dongle's bond ...
    assert path == f"{HCI0}/dev_50_98_93_FF_B4_D1"
    assert PAIRING._is_paired(objs, path) is True
    # ... but without an adapter scope it must not count as bonded.
    assert PAIRING._already_bonded(objs, path=path, adapter_path=None) is False


def test_bond_on_the_pairing_adapter_is_accepted() -> None:
    objs = _objects()
    path = PAIRING._find_device(
        objs, name=PANEL, address="50:98:93:FF:B4:D1", adapter_path=HCI0
    )
    assert PAIRING._already_bonded(objs, path=path, adapter_path=HCI0) is True


def test_scoping_to_the_other_adapter_finds_no_bond() -> None:
    objs = _objects()
    path = PAIRING._find_device(
        objs, name=PANEL, address="50:98:93:FF:B4:D1", adapter_path=HCI1
    )
    assert path is None
    assert PAIRING._already_bonded(objs, path=path, adapter_path=HCI1) is False


# --- pairing the RPA, not the identity address ----------------------------
#
# Observed on the van 2026-08-23 after the scoping fix: agent registered, then
# 60 s of silence and a timeout, with no Device1.Pair call ever reaching
# bluetoothd. Scoped to hci1, _find_device() looked for the identity address or
# the local name; in add-device mode the panel advertises a rotating RPA and no
# name, so nothing matched and the loop never had a path to pair.


class _BleDevice:
    def __init__(self, path: str) -> None:
        self.details = {"path": path}


def _with_resolved(path: str | None):
    """Run _live_device_path with the resolver pinned, then put it back.

    Restoring matters: ensure_bonded() calls the same resolver, so a leaked stub
    silently sends the dispatch tests down the proxy branch.
    """
    original = PAIRING.async_resolve_device
    PAIRING.async_resolve_device = lambda *a, **k: (
        _BleDevice(path) if path else None
    )
    try:
        return PAIRING._live_device_path(object(), PANEL, HCI1)
    finally:
        PAIRING.async_resolve_device = original


def test_live_path_finds_the_rpa_under_the_pairing_adapter() -> None:
    dev_path = f"{HCI1}/dev_49_3E_CD_8E_2F_8B"
    assert _with_resolved(dev_path) == dev_path


def test_live_path_rejects_a_device_on_another_adapter() -> None:
    # A route via the disabled dongle must not be paired on hci1.
    assert _with_resolved(f"{HCI0}/dev_49_3E_CD_8E_2F_8B") is None


def test_live_path_when_nothing_resolves() -> None:
    assert _with_resolved(None) is None


def test_live_path_without_hass_is_none() -> None:
    assert PAIRING._live_device_path(None, PANEL, HCI1) is None


if __name__ == "__main__":
    _install_stub()  # no pytest fixtures on this path
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
