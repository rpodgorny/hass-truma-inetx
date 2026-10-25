#!/usr/bin/env python3
"""Reconfigure must not dismantle a bond that is demonstrably working.

Measured on the van 2026-10-25 on 0.9.0b37: one ``Reconfigure`` submitted with
the panel idle on its normal screen unloaded the entry, called ``Pair()`` -- which
BlueZ answered without putting a single SMP frame on air -- and reloaded. Zero
refusals, bond intact, ~30 s of outage, and nothing learnt
(``research/truma-status-and-next-steps.md`` §4o).

So the flow now declines the attempt when the bond is carrying a session, and
says why. Three cases are pinned here, because getting any of them wrong turns a
UX fix into either a dead end or a lie:

1. **a live session** -> nothing is attempted, the entry is never unloaded, and
   the form re-shows with *forget the stale bond* as the way to insist;
2. **a bond that is not live** (the panel forgot us, the bus is deaf) -> the
   attempt runs exactly as before, because that is the only case a re-pair is
   for;
3. **a live session plus explicit consent** -> the attempt runs. The user's
   deliberate choice outranks our inference from a timestamp.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_passive_scan_discovery as H  # noqa: E402  (installs the HA stubs)

CONST, BT, CF = H._load()

PANEL = "Truma iNetX-FFB4D1"
ADDR = "50:98:93:FF:B4:D1"

# Every kwargs dict ensure_bonded was called with, and every form the step showed.
CALLS: list[dict] = []
SHOWN: dict = {}


class _Coordinator:
    """What the flow reads off ``entry.runtime_data``."""

    def __init__(self, live: bool, last_ok: float | None = None) -> None:
        self.session_live = live
        self.last_session_ok_at = last_ok


class _Entry:
    def __init__(self, coordinator: _Coordinator | None, state: str = "loaded") -> None:
        self.runtime_data = coordinator
        self.state = state
        self.entry_id = "E1"
        self.data = {"name": PANEL, "address": ADDR}


class _ConfigEntries:
    def __init__(self) -> None:
        self.unloaded: list[str] = []
        self.setup: list[str] = []

    async def async_unload(self, entry_id: str) -> bool:
        self.unloaded.append(entry_id)
        return True

    async def async_setup(self, entry_id: str) -> bool:
        self.setup.append(entry_id)
        return True


class _Hass(H._Hass):
    def __init__(self) -> None:
        super().__init__()
        self.config_entries = _ConfigEntries()


def _show(self, *, step_id, data_schema=None, **kw):
    SHOWN.clear()
    SHOWN.update(kw)
    SHOWN["step_id"] = step_id
    SHOWN["schema"] = data_schema
    raise H._Shown(step_id, data_schema)


async def _ensure_bonded(_hass, _name, _address, **kw):
    CALLS.append(kw)
    return False, None


# What the host is told it holds, per test. None = "cannot tell".
HOST_BOND: list[bool | None] = [None]


async def _host_bond_state(_name, _address, _adapter_path=None):
    return HOST_BOND[0]


H._ConfigFlow.async_show_form = _show
CF.ensure_bonded = _ensure_bonded
CF.async_host_bond_state = _host_bond_state


def _submit(
    entry: _Entry, user_input: dict | None, host_bond: bool | None = None
) -> tuple[object, _Hass]:
    """Drive one submit of the reconfigure pair step."""
    CALLS.clear()
    SHOWN.clear()
    HOST_BOND[0] = host_bond
    flow = CF.TrumaConfigFlow()
    flow.source = "reconfigure"
    flow._name = PANEL
    flow._address = ADDR
    flow.hass = hass = _Hass()
    flow._get_reconfigure_entry = lambda: entry
    try:
        asyncio.run(flow.async_step_pair(user_input))
    except H._Shown as shown:
        return shown, hass
    except H._Aborted as aborted:
        return aborted, hass
    raise AssertionError("the step neither showed a form nor aborted")


def _field_names(schema) -> list[str]:
    if schema is None:
        return []
    return [getattr(key, "schema", key) for key in schema.schema]


def test_a_live_bond_is_not_re_paired_without_consent() -> None:
    """The measured case: the panel is answering, so nothing is worth attempting."""
    shown, hass = _submit(_Entry(_Coordinator(live=True, last_ok=time.time())), {})
    assert CALLS == [], f"the flow paired over a working bond: {CALLS}"
    assert hass.config_entries.unloaded == [], (
        "the flow unloaded the entry for an attempt it should not have made"
    )
    assert isinstance(shown, H._Shown), shown
    assert SHOWN.get("errors", {}).get("base") == "bond_still_working", SHOWN
    # The consent checkbox must be there: it is the only way to insist.
    assert "forget_bond" in _field_names(SHOWN.get("schema")), SHOWN.get("schema")


def test_a_bond_that_is_not_live_is_still_re_paired() -> None:
    """The panel forgot us, or the bus is deaf: this is what a re-pair is for."""
    shown, hass = _submit(_Entry(_Coordinator(live=False, last_ok=None)), {})
    assert len(CALLS) == 1, f"no pairing attempt was made: {CALLS}"
    assert hass.config_entries.unloaded == ["E1"], "the entry must be unloaded: one dialler"


def test_a_stale_session_timestamp_is_not_proof_of_a_working_bond() -> None:
    """A session that ended hours ago proves nothing about the bond now."""
    old = time.time() - 3600
    shown, hass = _submit(_Entry(_Coordinator(live=False, last_ok=old)), {})
    assert len(CALLS) == 1, f"an hour-old session was treated as live: {CALLS}"


def test_consent_outranks_our_evidence() -> None:
    """A deliberate 'forget the stale bond' must always be able to run."""
    shown, hass = _submit(_Entry(_Coordinator(live=True, last_ok=time.time())),
                          {"forget_bond": True})
    assert len(CALLS) == 1, f"consent was ignored: {CALLS}"
    assert CALLS[0].get("may_forget_bond") is True, CALLS


def test_a_failed_attempt_on_a_bondless_host_names_the_clear_step() -> None:
    """No bond before the attempt is its own case, and its own instruction.

    Measured on the van 2026-10-26 (status file §4q): the state clears only
    after the panel's device list is emptied and the panel armed, so the generic
    "put it back into add-device mode" list is the wrong first step there.
    """
    shown, _hass = _submit(
        _Entry(_Coordinator(live=False, last_ok=None)), {}, host_bond=False
    )
    assert len(CALLS) == 1, f"the attempt should still have run: {CALLS}"
    assert SHOWN.get("errors", {}).get("base") == "pairing_failed_no_bond", SHOWN


def test_the_generic_message_survives_when_the_bond_state_is_unknown() -> None:
    """An unreadable bond state must not be reported as a missing bond."""
    shown, _hass = _submit(
        _Entry(_Coordinator(live=False, last_ok=None)), {}, host_bond=None
    )
    assert SHOWN.get("errors", {}).get("base") == "pairing_failed", SHOWN


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("reconfigure bond guard: all checks OK")


if __name__ == "__main__":
    main()
