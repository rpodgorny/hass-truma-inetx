#!/usr/bin/env python3
"""Offline check for the "the panel refuses us" repair issue.

No hardware: the harness in ``test_no_route_issue.py`` stubs Home Assistant and
loads the real ``const``/``bt``/``coordinator``, and this file reuses it.

Why this exists — measured, then reproduced on demand on the van 2026-10-25:

* the panel advertises normally, and the resolver finds it;
* a link comes up **unencrypted** and every connection dies;
* bluetoothd says ``Connection Rejected Due to Security Reasons`` (HCI 0x0E),
  because the panel no longer holds this host's key, while both adapters still
  hold a complete bond;
* Home Assistant only ever sees
  ``Failed to connect after 3 attempt(s): TimeoutError``;
* and the user was told **nothing at all** — no card, no warning, only a DEBUG
  line. ``repairs/list_issues`` was empty through 68 rejections.

Reproduction: clear the panel's own device list and leave its bond on this host.
That is a state the panel can also enter by itself (its Bluetooth stack crashed
earlier the same day, showing "bluetooth not available" on its screen).

What it pins:

1. silences do not count — a panel that is off, asleep or out of range produces
   the same timeout and needs the opposite advice,
2. three consecutive refusals *while the panel is advertising* raise the issue
   once, and only once, however long it goes on,
3. a session that carries data clears both the streak and the card,
4. the card is not "fixable" in the UI: the fix is a physical act on the panel
   plus a reconfigure, and pretending a button can do it would be a lie.

Run: ``python3 tests/test_panel_refusing_issue.py``
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location(
    "_truma_no_route_harness2", HERE / "test_no_route_issue.py"
)
assert spec is not None and spec.loader is not None
_h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = _h
spec.loader.exec_module(_h)

CONST, COORD, IR = _h.CONST, _h.COORD, _h.IR
ADVERTS = _h.ADVERTS
Info = _h._Info
PANEL = _h.PANEL


class _Coord:
    """Carries only what the method under test touches."""

    hass = object()
    unique_id = PANEL

    def __init__(self) -> None:
        self._refused_sessions = 0

    _note_session_refused = COORD.TrumaCoordinator._note_session_refused
    _async_clear_refused = COORD.TrumaCoordinator._async_clear_refused


def _advertising(panel_is_there: bool, age: float = 0.0) -> None:
    """Seed the advert store. ``age`` is how old the entry is, in seconds.

    Age matters: Home Assistant keeps an advert for hours, so "there is an
    entry" and "the panel is on air" are different claims.
    """
    ADVERTS[:] = [Info(name=PANEL, time=time.monotonic() - age)] if panel_is_there else []


def test_a_stale_cache_entry_is_not_a_refusal() -> None:
    """Measured on the van 2026-10-26, and it sent the user to destroy a bond.

    With the ESP proxy's advertisement stream stalled and no other scanner
    listening, every connect timed out. ``async_panel_advertising`` answered
    "yes" because the store still held the panel's last advert, three timeouts
    were counted as refusals, and the card told the user to clear the panel's
    device list. bluetoothd logged **zero** refusals in that window.
    """
    IR.__init__()
    _advertising(True, age=3600.0)  # in the cache, an hour old, nothing listening
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 3):
        c._note_session_refused()
    assert IR.creates == 0, "a stale advert must not accuse the panel"
    assert c._refused_sessions == 0, "and it must not count toward a later card"


def test_a_quiet_proxy_outranks_a_refusal() -> None:
    """A stalled proxy is ``proxy_quiet``'s business, not the panel's."""
    IR.__init__()
    _advertising(True)  # fresh advert, so freshness alone would allow the card
    original = COORD.quiet_proxy_scanners
    COORD.quiet_proxy_scanners = lambda hass, *a, **k: ["esp32-bluetooth-proxy-622700"]
    try:
        c = _Coord()
        for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 2):
            c._note_session_refused()
        assert IR.creates == 0, "the panel was not refusing; its transport was"
        assert c._refused_sessions == 0
    finally:
        COORD.quiet_proxy_scanners = original


def test_a_quiet_panel_is_not_a_refusal() -> None:
    """Off, asleep or out of range: same timeout, opposite advice."""
    IR.__init__()
    _advertising(False)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 3):
        c._note_session_refused()
    assert IR.creates == 0
    # Crucially it must not have counted either: otherwise a night with the panel
    # switched off trips the card on the first real refusal afterwards.
    assert c._refused_sessions == 0


def test_raised_once_when_the_panel_is_on_air_and_refusing() -> None:
    IR.__init__()
    _advertising(True)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE - 1):
        c._note_session_refused()
    assert IR.creates == 0
    c._note_session_refused()
    assert IR.creates == 1
    key = f"{CONST.DOMAIN}.{CONST.ISSUE_PANEL_REFUSING}"
    assert key in IR.active
    assert IR.active[key]["translation_key"] == CONST.ISSUE_PANEL_REFUSING
    assert IR.active[key]["is_fixable"] is False
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 20):
        c._note_session_refused()
    assert IR.creates == 1


def test_a_panel_that_comes_back_clears_the_card() -> None:
    IR.__init__()
    _advertising(True)
    c = _Coord()
    for _ in range(CONST.REFUSED_SESSIONS_BEFORE_ISSUE):
        c._note_session_refused()
    assert IR.creates == 1
    c._async_clear_refused()
    assert c._refused_sessions == 0
    assert IR.active == {}


def test_going_quiet_mid_streak_resets_it() -> None:
    """Two refusals, then the panel is switched off, then one refusal: no card."""
    IR.__init__()
    _advertising(True)
    c = _Coord()
    c._note_session_refused()
    c._note_session_refused()
    _advertising(False)
    c._note_session_refused()
    assert c._refused_sessions == 0
    _advertising(True)
    c._note_session_refused()
    assert IR.creates == 0


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("panel refusing issue: all checks OK")


if __name__ == "__main__":
    main()
