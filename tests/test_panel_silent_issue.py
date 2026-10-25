#!/usr/bin/env python3
"""Offline check for the "the panel is deaf" repair issue and back-off.

No hardware, no Home Assistant install: the harness in ``test_no_route_issue.py``
stubs Home Assistant and loads the real ``const``/``bt``/``coordinator``, and this
file reuses it rather than duplicating ninety lines of stubs.

Why this exists: measured on the van (2026-10-24/25), a panel can come up on a
link and then answer *nothing* -- "parameter discovery asked 18 device(s), 0
acknowledged; the link is up but carries nothing". The integration retried every
30/45 s forever, which on this hardware made it worse: after a handful of
reconnects the same panel needed a power cycle to recover, and the advice a user
needs ("the radio is fine, the panel is not") was never spoken. So the condition
is now its own failure, it backs off for half an hour, and it raises one repair
issue that says what to do.

What it pins:

1. ``BusSilent`` is still a ``StartupFailed`` -- every existing handler keeps
   working -- but is a distinct class, which is the only reason the coordinator
   can treat it differently,
2. a run shorter than the threshold says nothing (a single flat session is not
   evidence of a deaf panel),
3. the issue is raised exactly at the threshold and not re-raised by the
   sessions that follow, so an overnight outage leaves one card, not one per
   retry,
4. a session that actually carries traffic clears the streak and the issue,
5. the back-off is longer than the ordinary reconnect ceiling -- otherwise the
   whole mechanism is cosmetic.

Run: ``python3 tests/test_panel_silent_issue.py``
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _harness():
    """Load the sibling test module for its stubs and its loaded components."""
    spec = importlib.util.spec_from_file_location(
        "_truma_no_route_harness", HERE / "test_no_route_issue.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _harness()
CONST = H.CONST
COORD = H.COORD
IR = H.IR
# The coordinator imports ``session``, so the real module is already loaded.
SESSION = sys.modules["truma_pkg.session"]


class _Coord:
    """Carries only what the two methods under test touch."""

    hass = object()
    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self) -> None:
        self._silent_sessions = 0

    _note_session_silent = COORD.TrumaCoordinator._note_session_silent
    _async_clear_silent = COORD.TrumaCoordinator._async_clear_silent


def test_bus_silent_is_still_a_startup_failure() -> None:
    """Existing handlers must keep catching it; only the loop may special-case it."""
    assert issubclass(SESSION.BusSilent, SESSION.StartupFailed)
    assert SESSION.BusSilent is not SESSION.StartupFailed


def test_a_short_run_says_nothing() -> None:
    IR.__init__()
    c = _Coord()
    for _ in range(CONST.SILENT_SESSIONS_BEFORE_ISSUE - 1):
        c._note_session_silent()
    assert IR.creates == 0
    assert CONST.ISSUE_PANEL_SILENT not in "".join(IR.active)


def test_raised_once_at_the_threshold() -> None:
    IR.__init__()
    c = _Coord()
    for _ in range(CONST.SILENT_SESSIONS_BEFORE_ISSUE):
        c._note_session_silent()
    assert IR.creates == 1
    key = f"{CONST.DOMAIN}.{CONST.ISSUE_PANEL_SILENT}"
    assert key in IR.active
    assert IR.active[key]["translation_key"] == CONST.ISSUE_PANEL_SILENT
    # The panel is deaf, not the user's fault: it is not a "fix it" issue with
    # a config button, because the fix is a physical power cycle.
    assert IR.active[key]["is_fixable"] is False

    # A night of retries adds no cards: the counter is past the threshold and
    # the check is an equality, not a modulo.
    for _ in range(CONST.SILENT_SESSIONS_BEFORE_ISSUE * 20):
        c._note_session_silent()
    assert IR.creates == 1


def test_a_carrying_session_clears_it() -> None:
    IR.__init__()
    c = _Coord()
    for _ in range(CONST.SILENT_SESSIONS_BEFORE_ISSUE):
        c._note_session_silent()
    assert IR.creates == 1
    c._async_clear_silent()
    assert c._silent_sessions == 0
    assert IR.active == {}
    # And the next streak starts from scratch rather than one short of tripping.
    for _ in range(CONST.SILENT_SESSIONS_BEFORE_ISSUE - 1):
        c._note_session_silent()
    assert IR.creates == 1


def test_the_backoff_outlasts_the_ordinary_one() -> None:
    """If it did not, the deaf panel would be dialled as often as ever."""
    assert CONST.SILENT_BACKOFF > COORD._RECONNECT_DELAY_MAX
    # Long enough to be worth explaining to a user in minutes.
    assert CONST.SILENT_BACKOFF >= 600


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("panel silent issue: all checks OK")


if __name__ == "__main__":
    main()
