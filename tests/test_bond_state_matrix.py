#!/usr/bin/env python3
"""Lock the bond-state map of ``research/truma-one-sided-bond-matrix.md`` to the code.

The matrix's §2 table was written against **b26** and went stale silently over
twenty-one betas: it described the pairing path before b28's repair card, b38's
"decline to disturb a working bond" guard, b39's no-bond message and b46/b47's
"heard now, not in the cache" rule. A user-facing document that says what the
integration does has to be re-derived from the tree, or it becomes folklore.

So this file is a **specification lock**: one test per state S1-S8, each driving
the real coordinator method with the evidence that state actually produces, and
asserting the outcome documented in §2b (b47). If someone changes the
classification, a test here fails and whoever changed it has to update the
matrix in the same commit. That is the point — the table is a claim about
behaviour, so it belongs in a test.

**This file also pins four real gaps**, on purpose, so they cannot be forgotten:

* G1 — ``S3``/``S4``/``S5``/``S8`` are indistinguishable from ``S2`` to the
  coordinator, and all five raise ``panel_refusing``, whose first line says the
  *panel* has forgotten this Home Assistant. In S3/S5/S8 it is the **host** that
  is missing the key. ``pairing.async_host_bond_state`` exists (b35/b36) and is
  called by the config flow only, never on the session path — asserted below.
* G2 — ``bond_adapter`` is in the diagnostics download but no classification
  consults it, so S5/S6 get S2's cure, which would destroy a bond that is
  perfectly good on the other adapter.
* G3 — a signature-only record (S4) cannot be told from a complete one inside
  Home Assistant: ``Device1.Paired`` is true for both, and the completeness flag
  needs a root read of ``/var/lib/bluetooth``. So the *classification* is a
  documented limit rather than a bug — but the *sentence* was fixable and was
  fixed in b50: the card and its log line no longer assert that the panel forgot
  us, they name both holders and put the host-side re-pair first (S8 is a
  host-side defect, and the panel's list is not what repairs it). Pinned by
  ``test_g3_the_refusal_card_does_not_name_one_holder``.
* G4 — S7 (panel list full) is the one state whose advice the code already gets
  right, by accident: the host holds no bond at all, so b39's
  ``pairing_failed_no_bond`` names "clear the list, then arm" — which is S7's
  cure. Nothing detects ``list full'' itself.

None of these is fixed here. Per the project rule, a fix needs a reproduction on
the van, and S4/S5/S7/S8 have never been staged there.

Run: ``python3 tests/test_bond_state_matrix.py``
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location(
    "_truma_no_route_harness3", HERE / "test_no_route_issue.py"
)
assert spec is not None and spec.loader is not None
_h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = _h
spec.loader.exec_module(_h)

CONST, COORD, IR = _h.CONST, _h.COORD, _h.IR
ADVERTS = _h.ADVERTS
Info = _h._Info
PANEL = _h.PANEL

# The harness loads the modules by path rather than as a package, so source
# assertions read the files directly. Same tree, no import side effects.
SRC = HERE.parent / "custom_components" / "truma_inetx"

# Every issue key the integration knows, so "no such card" can be asserted
# positively rather than by absence of a line in a log.
ALL_ISSUES = {
    CONST.ISSUE_NO_ROUTE,
    CONST.ISSUE_HOST_DEAF,
    CONST.ISSUE_PROXY_QUIET,
    CONST.ISSUE_PANEL_SILENT,
    CONST.ISSUE_PANEL_REFUSING,
    CONST.ISSUE_SCANNER_PASSIVE,
}
# There is deliberately no ``host_bond_missing`` card: that is gap G1.
HOST_SIDE_BOND_ISSUES = {"host_bond_missing", "host_bond_stale"}


class _Coord:
    """Carries only what the methods under test touch."""

    hass = object()
    unique_id = PANEL

    def __init__(self) -> None:
        self._refused_sessions = 0
        self._host_bond_raised = False

    _note_session_refused = COORD.TrumaCoordinator._note_session_refused
    _async_clear_refused = COORD.TrumaCoordinator._async_clear_refused


def _advertising(panel_is_there: bool, age: float = 0.0) -> None:
    """Seed the advert store: fresh advert, stale advert, or nothing."""
    ADVERTS[:] = [Info(name=PANEL, time=time.monotonic() - age)] if panel_is_there else []


def _refuse(c, sessions: int) -> None:
    for _ in range(sessions):
        c._note_session_refused()


def _only_card() -> str:
    """The single issue currently raised, asserting there is exactly one."""
    assert len(IR.active) == 1, f"expected one card, got {sorted(IR.active)}"
    return IR.active[next(iter(IR.active))]["translation_key"]


# --------------------------------------------------------------------------
# S1 — panel has us, host complete. The healthy case.
# --------------------------------------------------------------------------


def test_s1_a_session_that_carries_data_raises_nothing() -> None:
    IR.__init__()
    _advertising(True)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE - 1)  # a blip on the way up
    assert IR.creates == 0
    c._async_clear_refused()  # the session carried data
    assert c._refused_sessions == 0
    assert IR.active == {}
    # And the streak really was forgotten, not just paused.
    _refuse(c, 1)
    assert IR.creates == 0, "one session after a good one must not trip the card"


# --------------------------------------------------------------------------
# S2 — panel forgot us, host complete. What the card is written for.
# --------------------------------------------------------------------------


def test_s2_panel_forgot_us_raises_panel_refusing() -> None:
    IR.__init__()
    _advertising(True)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE - 1)
    assert IR.creates == 0, "no card below the threshold"
    _refuse(c, 1)
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING


# --------------------------------------------------------------------------
# S3/S4/S5/S8 — the four states the host cannot distinguish from S2.
# G1/G2/G3 are asserted here; each assertion is the gap, not an endorsement.
# --------------------------------------------------------------------------


def test_s3_host_forgot_panel_now_names_the_host() -> None:
    """G1 — **CLOSED in 0.9.0b48** for the absence case.

    Stage 1 of this test asserted the gap: the host has no bond and the card
    said the *panel* had forgotten us. b48 added `host_bond_missing`, driven by
    `async_host_bond_state`'s absence-only answer, and the session loop now goes
    through the async note so that question is actually asked.

    What is still open is the *other* side of the same seam: a record BlueZ calls
    paired while it cannot encrypt (S8's IRK-less shape reports `Paired: yes`)
    still lands on `panel_refusing`. See the matrix §2c.
    """
    IR.__init__()
    _advertising(True)
    c = _Coord()
    # The host-side answer, when it is available, chooses the card…
    c._note_session_refused(host_bond=False)
    assert _only_card() == CONST.ISSUE_HOST_BOND_MISSING
    # …and the session loop is wired to ask for it rather than to guess.
    src = (SRC / "coordinator.py").read_text()
    assert "await self._async_note_session_refused()" in src
    assert "async_host_bond_state(self.unique_id, self.address)" in src


def test_s4_signature_only_is_indistinguishable_inside_home_assistant() -> None:
    """G3: a record BlueZ calls paired while it cannot encrypt.

    ⚠️ **Corrected 2026-10-26 by staging it on the van.** The state this test
    was written for — "Paired, but no LTK" — does not exist: dropping both LTK
    sections leaves BlueZ reporting **`Paired: no`**, because `Paired` is derived
    from the key material. So the absence *is* visible to the host, which is why
    the 0.9.0b48 `host_bond_missing` card caught exactly that state on the
    hardware (status file §4ag).

    What survives is the narrower reading asserted here: `_is_paired` consults
    `Device1.Paired` and nothing else, so any record BlueZ *does* call paired is
    taken at its word. No such state has been produced, and completeness cannot
    be checked over D-Bus (only by reading `/var/lib/bluetooth` as root).
    """
    IR.__init__()
    _advertising(True)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE)
    # With no host-side knowledge the refusal card is still what fires; b48 only
    # overrides it on a positive absence.
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING
    # The blind spot is in the reading, not in the card: _is_paired consults
    # Device1.Paired and nothing else, which is exactly why the completeness
    # check lives in the soak instrument (root file read) instead.
    src = (SRC / "pairing.py").read_text()
    is_paired = src.split("def _is_paired", 1)[1].split("\ndef ", 1)[0]
    assert "LongTermKey" not in is_paired
    assert "trusted" not in is_paired.lower()


def test_s5_bond_on_another_adapter_gets_s2s_cure() -> None:
    """G2: nothing consults which adapter holds the bond.

    In S5 a complete bond exists on the adapter Home Assistant is *not* dialling
    through. The card prescribes clearing the panel and re-pairing, which would
    throw that bond away; the cure this state needs is "get the bond onto the
    adapter in use". The diagnostics download carries ``bond_adapter`` (b35), so
    the information exists — no classification reads it.
    """
    IR.__init__()
    _advertising(True)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE)
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING

    src = (SRC / "coordinator.py").read_text()
    refused = src.split("def _note_session_refused", 1)[1].split("\n    def ", 1)[0]
    assert "bond_adapter" not in refused, (
        "the classification now reads bond_adapter — close G2 in the matrix"
    )


def test_s8_untrusted_or_irklss_record_also_gets_s2s_cure() -> None:
    """**Still open after b48** — and deliberately pinned as the remaining gap.

    Measured on the van 2026-10-26 by dropping the record's IRK: BlueZ reports
    `Paired: yes` (the LTK is still there), so `host_bond_missing` cannot fire and
    this state reaches `panel_refusing`. The cure it prescribes does repair it,
    because a re-pair writes a fresh IRK — so this was a wrong *sentence*, and
    b50 removed the wrong part: the card now names both holders and leads with
    the host-side re-pair (see the test below).

    Closing it needs the record's *contents*, which D-Bus does not expose: the
    only reader is the root-side file read the soak instrument already does. If
    that ever changes, this test fails and the matrix must change with it.
    """
    IR.__init__()
    _advertising(True)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE)
    assert _only_card() == CONST.ISSUE_PANEL_REFUSING
    assert not HOST_SIDE_BOND_ISSUES & set(IR.active)


def test_g3_the_refusal_card_does_not_name_one_holder() -> None:
    """The half of G3 that *can* be closed: the sentence, not the detection.

    Measured 2026-10-26 (§2c): dropping the record's IRK leaves ``Paired: yes``,
    so this state lands here and the broken half is the **host**. The card used
    to open with "the usual cause is that the panel no longer recognises this
    Home Assistant" and put "remove this Home Assistant from the panel's device
    list" as step 1 — a wrong claim and a physical act at the van that repairs
    nothing in S8, while a host-side re-pair repairs every state behind the card.

    So: two holders named in the head, and the host-side step first. The
    detection limit itself stays documented (see the test above).
    """
    import json

    for rel in ("strings.json", "translations/en.json", "translations/de.json"):
        data = json.loads((SRC / rel).read_text())
        desc = data["issues"]["panel_refusing"]["description"]
        head = desc.split("1.", 1)[0]
        low = head.lower()
        assert ("either" in low and " or " in low) or (
            "entweder" in low and " oder " in low
        ), f"{rel}: the head must name both holders, not one cause"
        assert "usual cause" not in desc.lower(), f"{rel}: still names one cause"
        assert "meistens liegt es daran" not in desc.lower(), (
            f"{rel}: still names one cause"
        )
        step1 = desc.split("1.", 1)[1].split("2.", 1)[0].lower()
        assert "reconfigure" in step1 or "neu konfigurieren" in step1, (
            f"{rel}: step 1 must be the host-side re-pair, not the panel's list"
        )


# --------------------------------------------------------------------------
# S6 — the same shape one hop out (bond on the proxy or the host, not both).
# --------------------------------------------------------------------------


def test_s6_a_quiet_proxy_owns_the_silence_instead() -> None:
    """S6 is split across two hosts; while the proxy is quiet it is its card."""
    IR.__init__()
    _advertising(True)  # even a fresh advert
    original = COORD.quiet_proxy_scanners
    COORD.quiet_proxy_scanners = lambda hass, *a, **k: ["esp32-bluetooth-proxy-622700"]
    try:
        c = _Coord()
        _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 2)
        assert IR.creates == 0, "the panel was not refusing; its transport was"
        assert c._refused_sessions == 0
    finally:
        COORD.quiet_proxy_scanners = original


# --------------------------------------------------------------------------
# S7 — panel list full. Flow-level, and the one state whose advice is right.
# --------------------------------------------------------------------------


def test_s7_the_flow_names_the_clear_then_arm_step() -> None:
    """G4: reached by accident, via b39's no-bond message.

    S7 has the panel's device list full (~4 entries), so a new bond is refused.
    The host holds no bond either, which is the case ``pairing_failed_no_bond``
    was written for — and that message's first step is "clear the panel's device
    list", which is exactly S7's cure. Nothing detects "list full" itself, and
    nothing here can: the panel does not report its list.
    """
    src = (SRC / "config_flow.py").read_text()
    assert '"pairing_failed_no_bond" if host_had_no_bond' in src
    strings = (SRC / "strings.json").read_text()
    assert "pairing_failed_no_bond" in strings


# --------------------------------------------------------------------------
# The inverse guard: a genuinely silent panel must not reach any of this.
# --------------------------------------------------------------------------


def test_a_panel_that_is_off_counts_towards_nothing() -> None:
    IR.__init__()
    _advertising(False)
    c = _Coord()
    _refuse(c, CONST.REFUSED_SESSIONS_BEFORE_ISSUE * 4)
    assert IR.creates == 0
    assert c._refused_sessions == 0
    assert not ALL_ISSUES & set(IR.active)


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
