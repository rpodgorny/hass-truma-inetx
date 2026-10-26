#!/usr/bin/env python3
"""The two defects the forged bond states exposed, pinned so they cannot come back.

Measured on the van 2026-10-26, by forging the host-side states that had never
been staged (`/root/truma-forge-bond-state.py`, restore = the undo):

1. **A session could stall silently.** With a signature-only record (S4) the
   connect *succeeded* and ``_start_notifications`` then never returned: 5 min
   52 s with no log line, no ACL to the panel, and — because nothing raised — no
   counted failure, so no repair card. The user saw 14 unavailable entities and
   an empty Repairs page. ``ble.connect`` now bounds establishment, so a stall
   becomes a failed attempt the coordinator can count.

2. **The only card that spoke accused the wrong party.** With the record's IRK
   removed (S8 variant) the panel refused every connection with HCI 0x0E while
   *this host* was the damaged side; the card said "the panel has most likely
   forgotten this Home Assistant" and sent the user to clear the panel's device
   list. ``_note_session_refused`` now asks BlueZ first and, on a positive
   absence (``Paired == False``), raises ``host_bond_missing`` instead.

The rules that keep (2) honest are tested here too: an *unknown* state keeps the
refusal card (unknown is not absent), and a host that does hold a bond is still
reported as a refusing panel.

Run: ``python3 tests/test_session_stall_and_host_bond.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import time
import types

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


# ---------------------------------------------------------------------------
# Part A — the establishment budget (ble.py)
# ---------------------------------------------------------------------------


def _load_ble():
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object)
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.characteristic", BleakGATTCharacteristic=object)
    _mod("bleak.backends.device", BLEDevice=object)
    _mod("bleak_retry_connector", BleakClientWithServiceCache=object,
         establish_connection=None)
    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])
    # ``ble`` imports the frame parser, which needs cbor2 — a third-party library
    # this test never exercises. Stubbed deliberately: the whole point of this
    # file is to run in the stage that has *no* third-party library installed.
    _mod("truma_pkg.truma.protocol", parse_v3_frame=lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location("truma_pkg.ble", SRC / "ble.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["truma_pkg.ble"] = module
    spec.loader.exec_module(module)
    return module


BLE = _load_ble()
PANEL_DEV = types.SimpleNamespace(name="Truma iNetX-FFB4D1", address="50:98:93:FF:B4:D1")


def test_a_stalled_establishment_raises_instead_of_hanging() -> None:
    """The measured 5 min 52 s silence, now bounded."""
    async def _never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    BLE.establish_connection = _never_returns
    BLE._ESTABLISH_BUDGET = 0.05
    client = BLE.TrumaBleClient({})
    started = time.monotonic()
    try:
        asyncio.run(client.connect(PANEL_DEV))
    except BLE.SessionStalled:
        pass
    else:
        raise AssertionError("a stalled establishment must raise, not hang")
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the budget did not bound it ({elapsed:.1f}s)"


def test_a_healthy_establishment_is_not_cut_short() -> None:
    """The budget must never fire early — 90 s against a 4-11 s healthy connect."""
    async def _connects(*args, **kwargs):
        await asyncio.sleep(0.01)
        return types.SimpleNamespace(is_connected=True, disconnect=_noop)

    async def _noop() -> None:
        return None

    BLE.establish_connection = _connects
    BLE._ESTABLISH_BUDGET = 5.0
    client = BLE.TrumaBleClient({})
    calls: list[int] = []

    async def _subscribe() -> None:
        calls.append(1)

    client._subscribe = _subscribe  # type: ignore[method-assign]
    asyncio.run(client.connect(PANEL_DEV))
    assert calls == [1], "connect() must still subscribe on the healthy path"


# ---------------------------------------------------------------------------
# Part B — which card a failed session raises (coordinator.py)
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("_harness_stall", HERE / "test_no_route_issue.py")
assert _spec is not None and _spec.loader is not None
_h = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _h
_spec.loader.exec_module(_h)

CONST, COORD, IR = _h.CONST, _h.COORD, _h.IR
ADVERTS, Info, PANEL = _h.ADVERTS, _h._Info, _h.PANEL


class _Coord:
    hass = object()
    unique_id = PANEL

    def __init__(self) -> None:
        self._refused_sessions = 0
        self._host_bond_raised = False

    _note_session_refused = COORD.TrumaCoordinator._note_session_refused
    _async_clear_refused = COORD.TrumaCoordinator._async_clear_refused


def _advertising(panel_is_there: bool, age: float = 0.0) -> None:
    ADVERTS[:] = [Info(name=PANEL, time=time.monotonic() - age)] if panel_is_there else []


def _only_card() -> str:
    assert len(IR.active) == 1, f"expected one card, got {sorted(IR.active)}"
    return IR.active[next(iter(IR.active))]["translation_key"]


def test_a_host_with_no_bond_names_the_host_not_the_panel() -> None:
    """The measured S8 case: 0x0E refusals, and the host was the damaged side."""
    IR.__init__()
    _advertising(True)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE):
        c._note_session_refused(host_bond=False)
    assert _only_card() == CONST.ISSUE_HOST_BOND_MISSING
    assert CONST.ISSUE_PANEL_REFUSING not in {v["translation_key"] for v in IR.active.values()}
    # And it is not a countdown: one session is enough, because the evidence is
    # the host's own record rather than three agreeing timeouts.
    assert IR.creates == 1


def test_the_host_card_does_not_need_a_fresh_advert() -> None:
    """Host-side evidence is independent of the airwaves."""
    IR.__init__()
    _advertising(False)  # panel switched off, or simply not heard
    c = _Coord()
    c._note_session_refused(host_bond=False)
    assert _only_card() == CONST.ISSUE_HOST_BOND_MISSING


def test_an_unknown_bond_state_keeps_the_refusal_card() -> None:
    """None means "could not ask" — that is not an absence."""
    IR.__init__()
    _advertising(True)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE - 1):
        c._note_session_refused(host_bond=None)
    assert IR.creates == 0
    c._note_session_refused(host_bond=None)
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING


def test_a_host_that_holds_a_bond_still_gets_the_refusal_card() -> None:
    """Paired == True is not proof of a *complete* record, so it changes nothing."""
    IR.__init__()
    _advertising(True)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE):
        c._note_session_refused(host_bond=True)
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING


def test_a_session_that_carries_data_clears_the_host_card() -> None:
    IR.__init__()
    _advertising(True)
    c = _Coord()
    c._note_session_refused(host_bond=False)
    assert IR.active
    c._async_clear_refused()
    assert IR.active == {}


def test_the_new_card_has_text_in_both_locales() -> None:
    import json

    for rel in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads((SRC / rel).read_text())
        card = data["issues"].get("host_bond_missing")
        assert card, f"{rel} is missing the host_bond_missing card"
        assert card["title"] and len(card["description"]) > 200, rel


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
