#!/usr/bin/env python3
"""Offline check for the "no Bluetooth proxy can reach the panel" repair issue.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``bt.async_panel_advertising`` and the real coordinator methods run.

Why this exists: the condition was already computed and thrown away (a debug
log line in ``bt.py``), so an ESP-less user saw entities sit unavailable with no
explanation. The risk in surfacing it is crying wolf -- warning on a transient
miss, or telling someone to buy a proxy when their panel is simply switched off.

What it pins:

1. the panel is recognised by service UUID as well as by local name (pairing
   adverts carry no name),
2. a run of failures shorter than the threshold stays silent,
3. the issue is raised exactly at the threshold and NOT re-raised afterwards,
4. a panel we cannot hear at all never raises it, and does not even count
   towards the threshold -- that is a different fault with different advice,
5. a successful resolve clears both the counter and the issue.

Run: ``python3 tests/test_no_proxy_issue.py``
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-FFB4D1"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Info:
    """Stand-in for a BluetoothServiceInfoBleak advert."""

    def __init__(
        self,
        name: str = "",
        uuids: tuple[str, ...] = (),
        address: str = "62:4A:BD:AD:73:5D",
        time: float = 0.0,
    ) -> None:
        self.name = name
        self.service_uuids = list(uuids)
        self.address = address
        self.time = time
        self.rssi = -70
        self.connectable = False


class _IssueRegistry:
    """Record create/delete calls the way HA's issue_registry would apply them."""

    class IssueSeverity:
        WARNING = "warning"
        ERROR = "error"

    def __init__(self) -> None:
        self.active: dict[str, dict] = {}
        self.creates = 0
        self.deletes = 0

    def async_create_issue(self, _hass, domain, issue_id, **kw):
        self.creates += 1
        self.active[f"{domain}.{issue_id}"] = kw

    def async_delete_issue(self, _hass, domain, issue_id):
        self.deletes += 1
        self.active.pop(f"{domain}.{issue_id}", None)


class _RemoteScanner:
    """Stands in for habluetooth.BaseHaRemoteScanner (an ESP32 proxy)."""


class _LocalScanner:
    """Stands in for a scanner backed by a host adapter."""


class _ScannerDevice:
    """Stand-in for a BluetoothScannerDevice: one advert seen by one scanner."""

    def __init__(self, address: str, remote: bool) -> None:
        self.scanner = _RemoteScanner() if remote else _LocalScanner()
        self.advertisement = _Info(address=address)
        self.ble_device = f"{'proxy' if remote else 'local'}:{address}"


ADVERTS: list[_Info] = []
SCANNERS: dict[str, list[_ScannerDevice]] = {}
IR = _IssueRegistry()


def _load():
    """Import the real const/bt/coordinator with externals stubbed out."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod("homeassistant.helpers", __path__=[], issue_registry=IR)
    _mod("homeassistant.helpers.storage", Store=object)
    _mod("homeassistant.components", __path__=[])
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=object)
    _mod("bleak.exc", BleakError=type("BleakError", (Exception,), {}))
    _mod(
        "bleak_retry_connector",
        BleakClientWithServiceCache=object,
        establish_connection=None,
    )

    class _Coordinator:
        """DataUpdateCoordinator stand-in that tolerates [TrumaState]."""

        def __class_getitem__(cls, _item):
            return cls

    _mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator)
    _mod(
        "homeassistant.components.bluetooth",
        async_discovered_service_info=lambda _hass, connectable=True: list(ADVERTS),
        async_scanner_devices_by_address=lambda _hass, address, connectable=True: list(
            SCANNERS.get(address, ())
        ),
    )
    # Without this, is_remote_scanner() hits ImportError and calls every scanner
    # local, which would silently pass the proxy-preference checks below.
    _mod("habluetooth", BaseHaRemoteScanner=_RemoteScanner)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", SERVICE_UUID=SERVICE_UUID, **dict.fromkeys(
        ("CTRL_MBP", "DEV_APP_DEFAULT", "DEV_HEATER", "DEV_PANEL",
         "MBP_PARAM_DISC", "TOPIC_BATCHES"), 0))
    _mod("truma_pkg.truma.protocol", **dict.fromkeys(
        ("build_identity_frames", "build_register_frame", "build_subscribe_frame",
         "build_v3_frame", "build_write_frame"), None))
    _mod("truma_pkg.truma.state", TrumaState=object)
    async def _no_bluez_device(_address):
        return None

    _mod("truma_pkg.ble", TrumaBleClient=object,
         device_from_bluez=_no_bluez_device)

    def _real(name: str):
        spec = importlib.util.spec_from_file_location(
            f"truma_pkg.{name}", SRC / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_pkg.{name}"] = module
        spec.loader.exec_module(module)
        return module

    const = _real("const")
    bt = _real("bt")
    coordinator = _real("coordinator")
    return const, bt, coordinator


CONST, BT, COORD = _load()


class _Coord:
    """Carries only what the two methods under test touch."""

    hass = object()
    unique_id = PANEL

    def __init__(self) -> None:
        self._no_proxy_misses = 0

    _async_note_no_proxy_route = COORD.TrumaCoordinator._async_note_no_proxy_route
    _async_clear_no_proxy_route = COORD.TrumaCoordinator._async_clear_no_proxy_route


def _set_adverts(*infos: _Info) -> None:
    ADVERTS[:] = infos


def test_panel_detection() -> None:
    _set_adverts()
    assert BT.async_panel_advertising(None, PANEL) is False

    _set_adverts(_Info(name=PANEL))
    assert BT.async_panel_advertising(None, PANEL) is True

    # Pairing/add-device adverts carry no local name -- the service UUID alone
    # must still identify the panel, or we would tell a pairing user their
    # panel is switched off.
    _set_adverts(_Info(uuids=(SERVICE_UUID,)))
    assert BT.async_panel_advertising(None, PANEL) is True

    # Somebody else's device must not look like our panel.
    _set_adverts(_Info(name="Some Other Thing", uuids=("0000180f-0000-1000-8000-00805f9b34fb",)))
    assert BT.async_panel_advertising(None, PANEL) is False


def test_debounced_warning() -> None:
    threshold = CONST.NO_PROXY_MISSES_BEFORE_WARNING
    assert threshold >= 2, "a threshold of 1 would warn on every transient miss"
    key = f"{CONST.DOMAIN}.{CONST.ISSUE_NO_PROXY_ROUTE}"

    IR.__init__()
    _set_adverts(_Info(name=PANEL))
    c = _Coord()

    for _ in range(threshold - 1):
        c._async_note_no_proxy_route()
    assert key not in IR.active, "warned before the debounce threshold"

    c._async_note_no_proxy_route()
    assert key in IR.active, "no issue raised at the threshold"
    assert IR.active[key]["severity"] == IR.IssueSeverity.WARNING
    assert IR.active[key]["is_fixable"] is False
    assert IR.active[key]["translation_key"] == CONST.ISSUE_NO_PROXY_ROUTE

    # Every later failure must stay quiet, or the user is re-notified on every
    # reconnect attempt for as long as the fault lasts.
    before = IR.creates
    for _ in range(5):
        c._async_note_no_proxy_route()
    assert IR.creates == before, "issue re-created after it was already raised"


def test_silent_when_panel_unheard() -> None:
    """A panel we cannot hear is a different fault -- never blame the proxy."""
    IR.__init__()
    _set_adverts()  # nothing audible
    c = _Coord()
    for _ in range(CONST.NO_PROXY_MISSES_BEFORE_WARNING * 3):
        c._async_note_no_proxy_route()
    assert IR.creates == 0
    # Crucially it must not have counted either: otherwise an out-of-range spell
    # pre-loads the counter and the next single miss trips the warning.
    assert c._no_proxy_misses == 0


def test_success_clears() -> None:
    IR.__init__()
    _set_adverts(_Info(name=PANEL))
    c = _Coord()
    for _ in range(CONST.NO_PROXY_MISSES_BEFORE_WARNING):
        c._async_note_no_proxy_route()
    assert IR.active

    c._async_clear_no_proxy_route()
    assert not IR.active, "issue survived a successful resolve"
    assert c._no_proxy_misses == 0

    # After clearing, the full threshold must elapse again before re-warning.
    for _ in range(CONST.NO_PROXY_MISSES_BEFORE_WARNING - 1):
        c._async_note_no_proxy_route()
    assert not IR.active


RPA = "62:4A:BD:AD:73:5D"
RPA2 = "7C:11:0E:22:91:04"
IDENTITY = "50:98:B8:FF:B4:D1"  # last three bytes == the panel's name suffix


def _set_route(*devices: _ScannerDevice) -> None:
    SCANNERS.clear()
    for device in devices:
        SCANNERS.setdefault(device.advertisement.address, []).append(device)


def test_proxy_wins_over_local() -> None:
    """A proxy route must be taken even when a local adapter also hears it.

    The proxy's controller resolves the panel's rotating address by itself, so
    it reconnects on any host; a local adapter needs LL Privacy or a patched
    kernel. Preferring the proxy also stops the host adapter from grabbing a
    connection the proxy is supposed to own.
    """
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(RPA, remote=True),
    )
    assert BT.async_resolve_proxy_device(None, PANEL) == f"proxy:{RPA}"


def test_local_used_when_no_proxy() -> None:
    """With no proxy in earshot, hand back the local adapter rather than None.

    On a stock kernel that link pairs but never reconnects -- which is what the
    repair issue explains. On a host whose kernel puts the peer's current RPA
    on air it works, and is the whole point of running proxy-less.
    """
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route(_ScannerDevice(RPA, remote=False))
    assert BT.async_resolve_proxy_device(None, PANEL) == f"local:{RPA}"


def test_none_when_unreachable() -> None:
    """Heard but nothing connectable -- the caller must retry, not connect."""
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route()
    assert BT.async_resolve_proxy_device(None, PANEL) is None


def test_identity_is_last_resort() -> None:
    """The identity address is tried only after every RPA, but IS tried.

    Via a proxy the identity usually dials a stale cached bond, so an RPA must
    win whenever one exists. While the panel is in add-device mode the identity
    is its real on-air address, so refusing it outright means never connecting.
    """
    _set_adverts(
        _Info(name=PANEL, address=IDENTITY, time=99.0),  # freshest on purpose
        _Info(name=PANEL, address=RPA, time=1.0),
    )
    _set_route(
        _ScannerDevice(IDENTITY, remote=True),
        _ScannerDevice(RPA, remote=True),
    )
    assert BT.async_resolve_proxy_device(None, PANEL) == f"proxy:{RPA}"

    _set_adverts(_Info(name=PANEL, address=IDENTITY))
    _set_route(_ScannerDevice(IDENTITY, remote=True))
    assert BT.async_resolve_proxy_device(None, PANEL) == f"proxy:{IDENTITY}"


def test_avoided_address_is_skipped() -> None:
    """A banished RPA must not come back as the local fallback either."""
    _set_adverts(
        _Info(name=PANEL, address=RPA, time=2.0),
        _Info(name=PANEL, address=RPA2, time=1.0),
    )
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(RPA2, remote=False),
    )
    assert BT.async_resolve_proxy_device(None, PANEL, avoid=[RPA]) == f"local:{RPA2}"


def test_advert_uuid_matches() -> None:
    """The panel must be recognised by the UUID it actually advertises.

    ``SERVICE_UUID`` is the GATT service and never appears in an advert, so
    matching on it alone leaves the panel invisible under passive scanning.
    """
    _set_adverts(_Info(uuids=(BT.ADVERT_SERVICE_UUID,), address=RPA))
    _set_route(_ScannerDevice(RPA, remote=True))
    assert BT.async_resolve_proxy_device(None, PANEL) == f"proxy:{RPA}"


def test_waits_for_a_fresh_advert() -> None:
    """The dial must not start on an address we have not heard just now.

    Without LL Privacy the host puts the address it last *saw* on air, and it
    only learns that while scanning -- which a connect attempt stops. Dialing
    on a stale record therefore burns a ~20 s timeout and keeps the record
    stale, which is the loop that left the panel down for hours on the van.
    """
    import asyncio
    import time as _time

    now = _time.monotonic()
    _set_adverts(_Info(name=PANEL, time=now))
    assert asyncio.run(BT.async_wait_until_heard(None, PANEL)) is True

    # Heard, but long enough ago that the host's cached address may have
    # rotated: refuse rather than dial it.
    _set_adverts(_Info(name=PANEL, time=now - 120))
    assert asyncio.run(BT.async_wait_until_heard(None, PANEL, timeout=0.1)) is False

    # Never heard at all.
    _set_adverts()
    assert asyncio.run(BT.async_wait_until_heard(None, PANEL, timeout=0.1)) is False


if __name__ == "__main__":
    test_panel_detection()
    test_debounced_warning()
    test_silent_when_panel_unheard()
    test_success_clears()
    test_proxy_wins_over_local()
    test_local_used_when_no_proxy()
    test_none_when_unreachable()
    test_identity_is_last_resort()
    test_avoided_address_is_skipped()
    test_advert_uuid_matches()
    test_waits_for_a_fresh_advert()
    print("no-proxy repair issue: all checks OK")
