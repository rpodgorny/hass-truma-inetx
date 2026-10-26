#!/usr/bin/env python3
"""Offline check for the "nothing can connect to the panel" repair issue.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``bt.async_panel_advertising`` and the real coordinator methods run.

Why this exists: the condition was already computed and thrown away (a debug
log line in ``bt.py``), so a user whose setup cannot reach the panel saw
entities sit unavailable with no explanation. The risk in surfacing it is
crying wolf -- warning on a transient miss, or blaming someone's Bluetooth
setup when their panel is simply switched off.

What it pins:

1. the panel is recognised by service UUID as well as by local name (pairing
   adverts carry no name),
2. a run of failures shorter than the threshold stays silent,
3. the issue is raised exactly at the threshold and NOT re-raised afterwards,
4. a panel we cannot hear at all never raises it, and does not even count
   towards the threshold -- that is a different fault with different advice,
5. a successful resolve clears both the counter and the issue.

Run: ``python3 tests/test_no_route_issue.py``
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from typing import TypedDict

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

PANEL = "Truma iNetX-FFB4D1"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"
# What the panel puts in the advertisement itself, as opposed to its GATT table.
ADVERT_SERVICE_UUID = "fc310000-f3b2-11e8-8eb2-f2801f1b9fd1"


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


class _Mode:
    """Stand-in for habluetooth's BluetoothScanningMode enum."""

    def __init__(self, name: str) -> None:
        self.name = name


class _StalledProxy(_RemoteScanner):
    """A proxy scanner whose own silence clock is what gives it away.

    The measured fault (2026-10-26): the ESP32 proxy kept answering its API and
    carrying other integrations' links while Home Assistant received nothing
    from it. ``time_since_last_detection`` is the only signal that names it,
    because the device cache still holds yesterday's entries.
    """

    def __init__(self, name: str = "esp32-bluetooth-proxy-622700", quiet: float = 900.0) -> None:
        self.name = name
        self.source = name
        self.connectable = True
        self.requested_mode = _Mode("auto")
        self.current_mode = _Mode("passive")
        self._quiet = quiet

    def time_since_last_detection(self) -> float:
        return self._quiet


class _LocalScannerWithClock(_LocalScanner):
    """A host adapter, equally silent -- but not a proxy, so not this card."""

    def __init__(self, name: str = "hci0", quiet: float = 900.0) -> None:
        self.name = name
        self.source = name
        self._quiet = quiet

    def time_since_last_detection(self) -> float:
        return self._quiet


class _Scanner:
    """A HaScanner, reduced to the two mode attributes that decide the advice."""

    def __init__(self, name: str, requested: str, current: str = "passive") -> None:
        self.name = name
        self.source = name
        self.requested_mode = _Mode(requested) if requested else None
        self.current_mode = _Mode(current) if current else None
        self.connectable = True


# The scanners this fake host has. Empty unless a test says otherwise, which
# keeps the pre-existing checks (no scan-mode data) unchanged.
MODES: list[_Scanner] = []
ADVERTS: list[_Info] = []
# The callback the integration registers to watch every advertisement.
WATCHERS: list = []


def _register_callback(_hass, callback, _matcher, _mode):
    WATCHERS.append(callback)

    def _unsub() -> None:
        WATCHERS.remove(callback)

    return _unsub
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
    # DeviceInfo is a TypedDict; at runtime this is the same thing. Declared
    # rather than aliased to plain ``dict`` because the coordinator asks it
    # which keys this Home Assistant takes, and ``dict`` has no annotations.
    class DeviceInfo(TypedDict, total=False):
        identifiers: set
        name: str
        via_device: tuple
        via_device_id: str

    _mod("homeassistant.helpers.device_registry", DeviceInfo=DeviceInfo)
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
        """DataUpdateCoordinator stand-in that tolerates [Bus]."""

        def __class_getitem__(cls, _item):
            return cls

    _mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator)
    _mod(
        "homeassistant.components.bluetooth",
        async_discovered_service_info=lambda _hass, connectable=True: list(ADVERTS),
        async_scanner_devices_by_address=lambda _hass, address, connectable=True: list(
            SCANNERS.get(address, ())
        ),
        async_current_scanners=lambda _hass: list(MODES),
        async_register_callback=_register_callback,
        BluetoothCallbackMatcher=lambda **kw: dict(kw),
        BluetoothScanningMode=type("M", (), {"PASSIVE": "passive", "ACTIVE": "active"}),
    )
    # Without this, is_remote_scanner() hits ImportError and calls every scanner
    # local, which would silently pass the proxy-preference checks below.
    _mod("habluetooth", BaseHaRemoteScanner=_RemoteScanner)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", SERVICE_UUID=SERVICE_UUID,
         DEVICE_SEED=frozenset(), MEASURE_REQUEST_TOPICS={}, DEV_PANEL=0x0101,
         DEV_BLE_MGMT=0x0601,
         **dict.fromkeys(
        ("CTRL_MBP", "DEV_APP_DEFAULT", "DEV_BROADCAST", "DEV_MSG_BROKER",
         "MBP_PARAM_DISC", "MEASURE_REQUEST_PARAM", "TOPIC_BATCHES"), 0))
    _mod("truma_pkg.truma.protocol", **dict.fromkeys(
        ("build_identity_frames", "build_register_frame", "build_subscribe_frame",
         "build_v3_frame", "build_write_frame"), None))
    async def _no_bluez_device(_address):
        return None

    async def _no_link_to_close(_client, _label):
        return None

    _mod("truma_pkg.ble", TrumaBleClient=object,
         device_from_bluez=_no_bluez_device, close_link=_no_link_to_close)

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
        self._no_route_misses = 0
        self._last_any_advert: float | None = None
        self._host_deaf_misses = 0
        self._proxy_quiet_misses = 0

    _async_note_no_route = COORD.TrumaCoordinator._async_note_no_route
    _async_clear_no_route = COORD.TrumaCoordinator._async_clear_no_route
    _async_note_host_deaf = COORD.TrumaCoordinator._async_note_host_deaf
    _async_any_advert = COORD.TrumaCoordinator._async_any_advert
    advert_quiet_for = COORD.TrumaCoordinator.advert_quiet_for


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
    threshold = CONST.NO_ROUTE_MISSES_BEFORE_WARNING
    assert threshold >= 2, "a threshold of 1 would warn on every transient miss"
    key = f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}"

    IR.__init__()
    _set_adverts(_Info(name=PANEL))
    c = _Coord()

    for _ in range(threshold - 1):
        c._async_note_no_route()
    assert key not in IR.active, "warned before the debounce threshold"

    c._async_note_no_route()
    assert key in IR.active, "no issue raised at the threshold"
    assert IR.active[key]["severity"] == IR.IssueSeverity.WARNING
    assert IR.active[key]["is_fixable"] is False
    assert IR.active[key]["translation_key"] == CONST.ISSUE_NO_ROUTE

    # Every later failure must stay quiet, or the user is re-notified on every
    # reconnect attempt for as long as the fault lasts.
    before = IR.creates
    for _ in range(5):
        c._async_note_no_route()
    assert IR.creates == before, "issue re-created after it was already raised"


def test_silent_when_panel_unheard() -> None:
    """A panel we cannot hear is a different fault -- never blame the radio."""
    IR.__init__()
    _set_adverts()  # nothing audible
    c = _Coord()
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING * 3):
        c._async_note_no_route()
    assert IR.creates == 0
    # Crucially it must not have counted either: otherwise an out-of-range spell
    # pre-loads the counter and the next single miss trips the warning.
    assert c._no_route_misses == 0


def test_success_clears() -> None:
    IR.__init__()
    _set_adverts(_Info(name=PANEL))
    c = _Coord()
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING):
        c._async_note_no_route()
    assert IR.active

    c._async_clear_no_route()
    assert not IR.active, "issue survived a successful resolve"
    assert c._no_route_misses == 0

    # After clearing, the full threshold must elapse again before re-warning.
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING - 1):
        c._async_note_no_route()
    assert not IR.active


RPA = "62:4A:BD:AD:73:5D"
RPA2 = "7C:11:0E:22:91:04"
IDENTITY = "50:98:B8:FF:B4:D1"  # last three bytes == the panel's name suffix


def _set_route(*devices: _ScannerDevice) -> None:
    SCANNERS.clear()
    for device in devices:
        SCANNERS.setdefault(device.advertisement.address, []).append(device)


def test_transport_is_not_chosen_here() -> None:
    """The resolver must not rank transports -- Home Assistant owns that.

    habluetooth keeps only the address from the BLEDevice we return and scores
    every connectable path again at connect time (RSSI, prior failures against
    that address on that scanner, connections in flight, free slots). A
    preference expressed here therefore buys nothing and cost issue #13's
    reporter minutes per connect: it decided which *address* was dialled, and
    on his host it kept picking one that never answers.
    """
    _set_adverts(_Info(name=PANEL, address=RPA))
    # Local first in the list: whatever is offered for the best address is
    # right, and no proxy may jump the queue.
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(RPA, remote=True),
    )
    assert BT.async_resolve_device(None, PANEL) == f"local:{RPA}"

    _set_route(
        _ScannerDevice(RPA, remote=True),
        _ScannerDevice(RPA, remote=False),
    )
    assert BT.async_resolve_device(None, PANEL) == f"proxy:{RPA}"


def test_local_only_host_is_served() -> None:
    """A host with no proxy in earshot gets its local adapter, as before."""
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route(_ScannerDevice(RPA, remote=False))
    assert BT.async_resolve_device(None, PANEL) == f"local:{RPA}"


def test_none_when_unreachable() -> None:
    """Heard but nothing connectable -- the caller must retry, not connect."""
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route()
    assert BT.async_resolve_device(None, PANEL) is None


def test_address_kind() -> None:
    """The identity address is the one whose tail is the name's suffix."""
    assert BT.address_kind(PANEL, IDENTITY) == BT.ADDR_IDENTITY
    assert BT.address_kind(PANEL, RPA) == BT.ADDR_RPA
    # A panel whose name carries no six-hex suffix gives nothing to match on,
    # so nothing may be claimed as the identity -- the iNet X Panel 2 (#6) is
    # discovered by service UUID and may be named anything at all.
    assert BT.address_kind("Truma iNetX", IDENTITY) == BT.ADDR_RPA


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
    assert BT.async_resolve_device(None, PANEL) == f"proxy:{RPA}"

    _set_adverts(_Info(name=PANEL, address=IDENTITY))
    _set_route(_ScannerDevice(IDENTITY, remote=True))
    assert BT.async_resolve_device(None, PANEL) == f"proxy:{IDENTITY}"


def test_an_address_the_panel_has_left_loses_to_a_live_one() -> None:
    """Issue #32: the kind preference is for addresses that are both on air.

    Measured on the van (2026-09-18). After a bond was dropped, an identity
    entry stayed in Home Assistant's cache with no signal behind it (rssi
    -127) while the panel advertised an RPA at -51. Ranking kind above
    freshness handed back the dead one every time -- for a whole 60 s pairing
    window, and before that for every reconnect at 45 s a go.
    """
    _set_adverts(
        _Info(name=PANEL, address=RPA, time=1000.0),
        _Info(name=PANEL, address=IDENTITY, time=100.0),  # left behind long ago
    )
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(IDENTITY, remote=False),
    )
    # Even for a host that has learned it connects on the identity address:
    # a memory says which kind answers, not that a dead entry is alive.
    assert (
        BT.async_resolve_device(None, PANEL, prefer_identity=True)
        == f"local:{RPA}"
    )
    # And it is still handed back when it is all there is -- rank, never
    # remove.
    _set_adverts(_Info(name=PANEL, address=IDENTITY, time=100.0))
    _set_route(_ScannerDevice(IDENTITY, remote=False))
    assert BT.async_resolve_device(None, PANEL) == f"local:{IDENTITY}"


def test_remembered_identity_is_dialled_first() -> None:
    """A host that connects on its identity address must not walk the RPAs.

    This is issue #13. On a kernel below 6.19 with the panel bonded to the
    local adapter, the identity address is the only one that ever answers --
    but it is ranked last, so every session first spent a 20 s connect timeout
    per advertised RPA. Measured on the reporter's Pi 5: 11.5 minutes from HA
    start to a live session, against 1.2 minutes dialling the working address
    first.
    """
    _set_adverts(
        _Info(name=PANEL, address=RPA, time=99.0),  # freshest, and useless here
        _Info(name=PANEL, address=IDENTITY, time=1.0),
    )
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(IDENTITY, remote=False),
    )
    assert BT.async_resolve_device(None, PANEL) == f"local:{RPA}"
    assert (
        BT.async_resolve_device(None, PANEL, prefer_identity=True)
        == f"local:{IDENTITY}"
    )


def test_preference_reorders_but_never_excludes() -> None:
    """A memory that no longer fits must cost one attempt, not the connection.

    The panel is not advertising its identity at all here (the usual case
    between add-device sessions), so a host remembering the identity has to
    fall through to the RPAs rather than reporting the panel unreachable --
    which would raise the "nothing can reach it" repair issue against a panel
    that is plainly on air.
    """
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route(_ScannerDevice(RPA, remote=False))
    assert (
        BT.async_resolve_device(None, PANEL, prefer_identity=True) == f"local:{RPA}"
    )


def test_avoid_outranks_the_preference() -> None:
    """A failed address sinks below everything, preferred kind or not.

    Otherwise the post-pairing phantom -- or an identity address the panel is
    briefly refusing because it still holds the slot of a just-closed session
    -- would be handed back every round to a host that remembers it, and the
    rotation the avoid set exists for would never happen.
    """
    _set_adverts(
        _Info(name=PANEL, address=IDENTITY, time=99.0),
        _Info(name=PANEL, address=RPA, time=1.0),
    )
    _set_route(
        _ScannerDevice(IDENTITY, remote=False),
        _ScannerDevice(RPA, remote=False),
    )
    assert (
        BT.async_resolve_device(
            None, PANEL, avoid=[IDENTITY], prefer_identity=True
        )
        == f"local:{RPA}"
    )


def test_avoided_address_is_demoted() -> None:
    """A failed RPA loses to every other candidate, on either transport.

    This is the rotation the avoid set exists for: the post-pairing phantom is
    the fresher of the two adverts, so without the demotion it wins forever.
    """
    _set_adverts(
        _Info(name=PANEL, address=RPA, time=2.0),
        _Info(name=PANEL, address=RPA2, time=1.0),
    )
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(RPA2, remote=False),
    )
    assert BT.async_resolve_device(None, PANEL, avoid=[RPA]) == f"local:{RPA2}"


def test_avoided_identity_is_still_offered() -> None:
    """The identity address must survive the avoid set (issue #14).

    It never rotates, so it can never be the post-pairing phantom the set was
    built for. Excluding it meant one transient failure -- the panel still
    holding the slot of a just-closed session, say -- erased the only route a
    host that connects over the identity has, and the host then sat there
    unreachable until something else cleared the set.
    """
    _set_adverts(_Info(name=PANEL, address=IDENTITY))
    _set_route(_ScannerDevice(IDENTITY, remote=False))
    assert (
        BT.async_resolve_device(None, PANEL, avoid=[IDENTITY])
        == f"local:{IDENTITY}"
    )


def test_avoided_rpa_loses_to_the_identity() -> None:
    """A failed RPA must rank below the identity, fresher or not.

    The identity is otherwise the last resort, so the only way it gets tried on
    a host where it is the working route is by the RPAs demoting themselves.
    """
    _set_adverts(
        _Info(name=PANEL, address=RPA, time=2.0),
        _Info(name=PANEL, address=IDENTITY, time=1.0),
    )
    _set_route(
        _ScannerDevice(RPA, remote=False),
        _ScannerDevice(IDENTITY, remote=False),
    )
    assert (
        BT.async_resolve_device(None, PANEL, avoid=[RPA]) == f"local:{IDENTITY}"
    )


def test_sole_avoided_address_is_retried() -> None:
    """The last route left is handed back even after it failed.

    The set cannot tell a phantom from a failure that heals, and returning
    ``None`` here would report "not advertising" for a panel we can plainly
    hear -- which is what raises the "you need a proxy" repair issue.
    """
    _set_adverts(_Info(name=PANEL, address=RPA))
    _set_route(_ScannerDevice(RPA, remote=True))
    assert BT.async_resolve_device(None, PANEL, avoid=[RPA]) == f"proxy:{RPA}"


def test_advert_uuid_matches() -> None:
    """The panel must be recognised by the UUID it actually advertises.

    ``SERVICE_UUID`` is the GATT service and never appears in an advert, so
    matching on it alone leaves the panel invisible under passive scanning.
    """
    _set_adverts(_Info(uuids=(ADVERT_SERVICE_UUID,), address=RPA))
    _set_route(_ScannerDevice(RPA, remote=True))
    assert BT.async_resolve_device(None, PANEL) == f"proxy:{RPA}"


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


# --- address-kind memory ---------------------------------------------------
# The resolver ranks addresses; the coordinator is what remembers which kind
# this host actually connects on, and what unlearns it when that stops being
# true. Both halves matter: without the memory a host pays issue #13's connect
# cost on every session, and without the unlearning a host that loses its
# route (a kernel upgrade taking the local adapter away) never finds the other
# one and looks like broken hardware.


class _FakeStore:
    """Records the last save, the way HA's Store would persist it."""

    def __init__(self) -> None:
        self.saved: dict | None = None

    async def async_save(self, data: dict) -> None:
        self.saved = dict(data)


class _MemoryCoord:
    """Carries only what the address-kind memory touches."""

    unique_id = PANEL

    def __init__(self, kind: str | None = None) -> None:
        self._address_kind = kind
        self._kind_stale = False
        self._last_kind: str | None = None
        self._last_addr: str | None = None
        self._session_ok = False
        self._avoid: set[str] = set()
        self._stored: dict = {}
        self._store = _FakeStore()

    _prefer_identity = COORD.TrumaCoordinator._prefer_identity
    _note_attempt_failed = COORD.TrumaCoordinator._note_attempt_failed
    _remember_address_kind = COORD.TrumaCoordinator._remember_address_kind


def test_no_memory_keeps_the_old_order() -> None:
    """A fresh install must behave exactly as it did: RPAs first."""
    assert _MemoryCoord()._prefer_identity() is False


def test_memory_is_followed_and_alternates_on_failure() -> None:
    """Follow what worked; after it fails, try the other kind, then back."""
    c = _MemoryCoord(BT.ADDR_IDENTITY)
    assert c._prefer_identity() is True

    # The remembered kind failed to carry a session -- try the other one next.
    c._last_kind = BT.ADDR_IDENTITY
    c._note_attempt_failed()
    assert c._prefer_identity() is False

    # ...and when that one fails too, come back rather than sticking with it.
    c._last_kind = BT.ADDR_RPA
    c._note_attempt_failed()
    assert c._prefer_identity() is True


def test_a_dropped_session_does_not_flip_the_memory() -> None:
    """Hours of good service then a drop says nothing about the address kind.

    Without this guard a single mid-session disconnect -- the panel rebooting,
    the van's power going off -- would send the next connect at the kind that
    has never worked on this host, for no evidence at all.
    """
    c = _MemoryCoord(BT.ADDR_IDENTITY)
    c._last_kind = BT.ADDR_IDENTITY
    c._session_ok = True
    c._note_attempt_failed()
    assert c._kind_stale is False
    assert c._prefer_identity() is True


def test_an_attempt_that_dialled_nothing_teaches_nothing() -> None:
    """A round that never picked an address must not touch the memory.

    ``_connect_and_run`` clears the kind at the top of every attempt, so this
    is what a failure looks like when the panel was silent or nothing
    connectable could reach it -- a fault about the panel or the radio, saying
    nothing whatever about which address kind answers on this host.
    """
    c = _MemoryCoord(BT.ADDR_IDENTITY)
    c._last_kind = None
    c._note_attempt_failed()
    assert c._kind_stale is False
    assert c._prefer_identity() is True


def test_failed_address_is_still_demoted() -> None:
    """The avoid half of the same method must keep working."""
    c = _MemoryCoord()
    c._last_addr = RPA
    c._note_attempt_failed()
    assert c._avoid == {RPA}


def test_remembering_persists_once() -> None:
    """The memory is written to storage, and only when it changes.

    Persisted because the cost it avoids is paid at startup, and a memory that
    lived only in RAM would be relearned -- slowly -- after every restart.
    """
    import asyncio

    c = _MemoryCoord()
    c._kind_stale = True
    asyncio.run(c._remember_address_kind(BT.ADDR_IDENTITY))
    assert c._address_kind == BT.ADDR_IDENTITY
    assert c._store.saved == {"address_kind": BT.ADDR_IDENTITY}
    # A success also means the memory is trustworthy again.
    assert c._kind_stale is False

    c._store.saved = None
    asyncio.run(c._remember_address_kind(BT.ADDR_IDENTITY))
    assert c._store.saved is None, "rewrote storage for an unchanged memory"


# --- poll mode -------------------------------------------------------------
# Not about proxies, but this file already stands the coordinator up and the
# option is one property plus one branch; duplicating the harness to test two
# lines would be worse than the small mismatch in scope.

def _poll_interval_of(options: dict) -> int:
    """Call the property without building a whole coordinator."""
    prop = COORD.TrumaCoordinator.poll_interval.fget
    entry = type("E", (), {"options": options})()
    return prop(type("C", (), {"config_entry": entry})())


def test_poll_interval_defaults_to_staying_connected() -> None:
    """No option set must mean the old behaviour, not a surprise poll."""
    assert _poll_interval_of({}) == 0


def test_poll_interval_is_read_from_options() -> None:
    assert _poll_interval_of({"poll_interval_seconds": 60}) == 60
    # HA hands numbers back as strings from some form backends
    assert _poll_interval_of({"poll_interval_seconds": "90"}) == 90


def test_card_names_the_reload_first() -> None:
    """The measured cure must come before kernel archaeology.

    Measured on the van 2026-10-25: the panel was fresh on air (rssi -53, seconds
    old) and Home Assistant's own advert store held it under the *identity*
    address with ``connectable: true``, while
    ``bluetooth.async_scanner_devices_by_address(<identity>, connectable=True)``
    returned nothing -- the two views had stopped agreeing. Reloading the
    Bluetooth integration made the session come up immediately, with nothing
    changed on the host or the panel.

    So a card that opens with kernel versions and LL Privacy sends the user to
    buy a proxy over a one-click fix. The *order* is the assertion here, not
    just the presence of the words.
    """
    import json

    desc = json.loads((SRC / "strings.json").read_text())["issues"]["no_route"][
        "description"
    ]
    low = desc.lower()
    assert "reload" in low, "the card never mentions the one-click cure"
    assert "bluetooth" in low
    assert low.index("reload") < low.index("kernel"), (
        "host-capability detail is offered before the one-click cure"
    )
    # Charter C7: no user-facing text may send anyone to a privileged command.
    for forbidden in ("bluetoothd", "hciconfig", "systemctl restart"):
        assert forbidden not in low, f"{forbidden} must not appear in a repair card"

    shipped = json.loads((SRC / "translations" / "en.json").read_text())["issues"][
        "no_route"
    ]["description"]
    assert shipped == desc, "translations/en.json drifted from strings.json"


def test_a_passively_configured_host_names_the_scan_mode() -> None:
    """Heard but nameless on a passive-only host is not a resolver fault.

    Measured chain (KB, ble-passive-scan-bug-upstream-brief): the controller
    refused `LE Set Scan Parameters` (0x2013, EPROTO), the stack silently fell
    back to passive, and no scan response -- hence no name -- ever arrived.
    Telling that user to reload Bluetooth is advice that cannot work, because
    the request is never made.
    """
    IR.__init__()
    MODES[:] = [_Scanner("hci0", "passive")]
    _set_adverts(_Info(uuids=(SERVICE_UUID,)))  # heard, no name
    coord = _Coord()
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_SCANNER_PASSIVE}" in IR.active, IR.active
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}" not in IR.active, IR.active
    assert "hci0" in str(IR.active[f"{CONST.DOMAIN}.{CONST.ISSUE_SCANNER_PASSIVE}"])


def test_an_active_capable_host_still_gets_the_no_route_card() -> None:
    """AUTO hosts are passive most of the time and must not raise the scanner card."""
    IR.__init__()
    MODES[:] = [_Scanner("hci0", "active", current="passive")]
    _set_adverts(_Info(uuids=(SERVICE_UUID,)))
    coord = _Coord()
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}" in IR.active, IR.active
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_SCANNER_PASSIVE}" not in IR.active, IR.active


def test_a_named_panel_never_gets_the_scanner_card() -> None:
    """If the name is arriving, the scanning mode is not the problem."""
    IR.__init__()
    MODES[:] = [_Scanner("hci0", "passive")]
    _set_adverts(_Info(name=PANEL))
    coord = _Coord()
    for _ in range(CONST.NO_ROUTE_MISSES_BEFORE_WARNING):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_SCANNER_PASSIVE}" not in IR.active, IR.active


def test_the_scanner_card_ships_in_both_languages() -> None:
    """A card with no text behind it is worse than no card at all."""
    for lang in ("en", "de"):
        issues = json.loads((SRC / "translations" / f"{lang}.json").read_text())["issues"]
        entry = issues[CONST.ISSUE_SCANNER_PASSIVE]
        assert entry["title"] and entry["description"], lang
        assert "{scanners}" in entry["description"], f"{lang} lost the placeholder"


def test_a_deaf_host_is_named_once_nothing_is_heard() -> None:
    """Panel unheard AND nothing else heard: the radio, not the panel."""
    IR.__init__()
    MODES[:] = []
    _set_adverts()  # panel not heard; nothing else either
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - (CONST.HOST_DEAF_QUIET_SECONDS + 60)
    for _ in range(CONST.HOST_DEAF_MISSES_BEFORE_ISSUE):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}" in IR.active, IR.active
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}" not in IR.active, IR.active


def test_a_panel_that_is_merely_off_is_not_a_deaf_host() -> None:
    """Other devices still arriving means the radio works and the panel is off."""
    IR.__init__()
    _set_adverts()
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - 2.0
    for _ in range(CONST.HOST_DEAF_MISSES_BEFORE_ISSUE * 3):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}" not in IR.active, IR.active
    assert coord._host_deaf_misses == 0, "a live radio must reset the deaf streak"


def test_without_a_baseline_the_host_stays_quiet() -> None:
    """Never having heard anything is unknowable, not deafness."""
    IR.__init__()
    _set_adverts()
    coord = _Coord()
    for _ in range(CONST.HOST_DEAF_MISSES_BEFORE_ISSUE * 3):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}" not in IR.active, IR.active
    assert coord.advert_quiet_for is None


def test_the_watcher_stamps_the_clock_and_the_card_clears() -> None:
    """The registered callback is what makes 'anything heard' knowable."""
    IR.__init__()
    _set_adverts()
    coord = _Coord()
    WATCHERS.clear()
    unsub = BT.async_watch_adverts(object(), coord._async_any_advert)
    assert WATCHERS, "no advertisement watcher was registered"
    assert coord.advert_quiet_for is None
    WATCHERS[0](None, None)          # an advert arrives
    assert coord.advert_quiet_for is not None and coord.advert_quiet_for < 1.0
    # ...and a healthy resolve drops the card and the streak.
    IR.active[f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}"] = {}
    coord._host_deaf_misses = CONST.HOST_DEAF_MISSES_BEFORE_ISSUE
    coord._async_clear_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}" not in IR.active
    assert coord._host_deaf_misses == 0
    unsub()
    assert not WATCHERS


def test_a_stalled_proxy_gets_its_own_card() -> None:
    """A proxy that delivers nothing is not a host that hears nothing.

    Measured on the van 2026-10-26, twice in one afternoon: the ESP32 proxy kept
    answering its own API and kept carrying the Renogy links, while Home
    Assistant received no advertisements from it at all -- "Bluetooth scanner
    has gone quiet for 1331.8s". Reloading the *Bluetooth* integration did not
    bring it back; reloading the proxy's **ESPHome** entry did, in seconds. Two
    faults, two cures, so two cards.
    """
    IR.__init__()
    MODES[:] = [_StalledProxy()]
    _set_adverts()  # the panel is not heard either
    coord = _Coord()
    # Other devices are still arriving: this is NOT a deaf host.
    coord._last_any_advert = time.monotonic() - 2.0
    for _ in range(CONST.PROXY_QUIET_MISSES_BEFORE_ISSUE):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" in IR.active, IR.active
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_HOST_DEAF}" not in IR.active, IR.active
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}" not in IR.active, IR.active
    card = IR.active[f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}"]
    assert "esp32-bluetooth-proxy-622700" in card["translation_placeholders"]["proxies"]


def test_a_proxy_that_is_still_delivering_is_not_blamed() -> None:
    """A working proxy stays quiet: the clock, not the mere existence, decides."""
    IR.__init__()
    MODES[:] = [_StalledProxy(quiet=5.0)]
    _set_adverts()
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - 2.0
    for _ in range(CONST.PROXY_QUIET_MISSES_BEFORE_ISSUE * 3):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" not in IR.active, IR.active
    assert coord._proxy_quiet_misses == 0


def test_one_silent_sample_does_not_raise_the_proxy_card() -> None:
    """It has to agree over more than one connect round."""
    IR.__init__()
    MODES[:] = [_StalledProxy()]
    _set_adverts()
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - 2.0
    coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" not in IR.active, IR.active


def test_a_silent_host_adapter_is_not_a_stalled_proxy() -> None:
    """``BaseHaRemoteScanner`` is the discriminator, not a name or a clock."""
    IR.__init__()
    MODES[:] = [_LocalScannerWithClock()]
    _set_adverts()
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - 2.0
    for _ in range(CONST.PROXY_QUIET_MISSES_BEFORE_ISSUE * 3):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" not in IR.active, IR.active


def test_the_proxy_card_clears_when_the_panel_answers() -> None:
    """A successful resolve drops every card this path raised."""
    IR.__init__()
    MODES[:] = [_StalledProxy()]
    _set_adverts()
    coord = _Coord()
    coord._last_any_advert = time.monotonic() - 2.0
    for _ in range(CONST.PROXY_QUIET_MISSES_BEFORE_ISSUE):
        coord._async_note_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" in IR.active, IR.active
    coord._async_clear_no_route()
    assert f"{CONST.DOMAIN}.{CONST.ISSUE_PROXY_QUIET}" not in IR.active, IR.active
    assert coord._proxy_quiet_misses == 0


def test_the_proxy_card_ships_in_both_languages() -> None:
    """Repair cards are product text: every locale needs the block."""
    for locale in ("en", "de"):
        path = SRC / "translations" / f"{locale}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        block = data["issues"]["proxy_quiet"]
        assert block["title"], f"{locale}: empty title"
        assert "{proxies}" in block["description"], f"{locale}: no placeholder"


def test_the_proxy_card_names_the_cause_before_the_cure() -> None:
    """The other tool comes first, because closing it alone does not help.

    Measured 2026-10-26: a second API client subscribed to the proxy's raw
    advertisement stream starved Home Assistant's own (its scanner went from
    0.1 s quiet to 182 s and climbing), and the stream did **not** come back
    when that client detached -- 302 s of silence until the ESPHome entry was
    reloaded. So the advice has to be "close it, then reload": a card that says
    only "reload" would be undone the moment the other tool reconnects.
    """
    data = json.loads((SRC / "translations" / "en.json").read_text(encoding="utf-8"))
    desc = data["issues"]["proxy_quiet"]["description"].lower()
    assert "another tool" in desc, "the cause is not named"
    assert desc.index("another tool") < desc.index("reload"), (
        "the card must name the other tool before it says to reload"
    )
    for privileged in ("bluetoothd", "hciconfig", "systemctl restart"):
        assert privileged not in desc, f"user-facing text names {privileged}"


if __name__ == "__main__":
    test_panel_detection()
    test_debounced_warning()
    test_silent_when_panel_unheard()
    test_success_clears()
    test_transport_is_not_chosen_here()
    test_local_only_host_is_served()
    test_none_when_unreachable()
    test_address_kind()
    test_identity_is_last_resort()
    test_an_address_the_panel_has_left_loses_to_a_live_one()
    test_remembered_identity_is_dialled_first()
    test_preference_reorders_but_never_excludes()
    test_avoid_outranks_the_preference()
    test_avoided_address_is_demoted()
    test_avoided_identity_is_still_offered()
    test_avoided_rpa_loses_to_the_identity()
    test_sole_avoided_address_is_retried()
    test_no_memory_keeps_the_old_order()
    test_memory_is_followed_and_alternates_on_failure()
    test_a_dropped_session_does_not_flip_the_memory()
    test_an_attempt_that_dialled_nothing_teaches_nothing()
    test_failed_address_is_still_demoted()
    test_remembering_persists_once()
    test_advert_uuid_matches()
    test_waits_for_a_fresh_advert()
    test_poll_interval_defaults_to_staying_connected()
    test_poll_interval_is_read_from_options()
    test_card_names_the_reload_first()
    test_a_passively_configured_host_names_the_scan_mode()
    test_an_active_capable_host_still_gets_the_no_route_card()
    test_a_stalled_proxy_gets_its_own_card()
    test_a_proxy_that_is_still_delivering_is_not_blamed()
    test_one_silent_sample_does_not_raise_the_proxy_card()
    test_a_silent_host_adapter_is_not_a_stalled_proxy()
    test_the_proxy_card_clears_when_the_panel_answers()
    test_the_proxy_card_ships_in_both_languages()
    test_the_proxy_card_names_the_cause_before_the_cure()
    test_a_named_panel_never_gets_the_scanner_card()
    test_the_scanner_card_ships_in_both_languages()
    test_a_deaf_host_is_named_once_nothing_is_heard()
    test_a_panel_that_is_merely_off_is_not_a_deaf_host()
    test_without_a_baseline_the_host_stays_quiet()
    test_the_watcher_stamps_the_clock_and_the_card_clears()
    print("no-route repair issue: all checks OK")
