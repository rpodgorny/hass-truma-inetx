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

    def __init__(self, name: str = "", uuids: tuple[str, ...] = ()) -> None:
        self.name = name
        self.service_uuids = list(uuids)
        self.address = "62:4A:BD:AD:73:5D"
        self.time = 0.0
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


ADVERTS: list[_Info] = []
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
        async_scanner_devices_by_address=lambda *a, **k: [],
    )

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", SERVICE_UUID=SERVICE_UUID, **dict.fromkeys(
        ("CTRL_MBP", "DEV_APP_DEFAULT", "DEV_HEATER", "DEV_PANEL",
         "MBP_PARAM_DISC", "TOPIC_BATCHES"), 0))
    _mod("truma_pkg.truma.protocol", **dict.fromkeys(
        ("build_identity_frames", "build_register_frame", "build_subscribe_frame",
         "build_v3_frame", "build_write_frame"), None))
    _mod("truma_pkg.truma.state", TrumaState=object)
    _mod("truma_pkg.ble", TrumaBleClient=object)

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


if __name__ == "__main__":
    test_panel_detection()
    test_debounced_warning()
    test_silent_when_panel_unheard()
    test_success_clears()
    print("no-proxy repair issue: all checks OK")
