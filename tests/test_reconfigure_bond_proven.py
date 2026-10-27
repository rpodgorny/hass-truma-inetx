#!/usr/bin/env python3
"""What the pairing step must refuse, and what it must never blame the panel for.

Part 1 — **Reconfigure must not dismantle a bond that is demonstrably working.**

Measured on the van 2026-10-25 on 0.9.0b37: one ``Reconfigure`` submitted with
the panel idle on its normal screen unloaded the entry, called ``Pair()`` -- which
BlueZ answered without putting a single SMP frame on air -- and reloaded. Zero
refusals, bond intact, ~30 s of outage, and nothing learnt
(``research/truma-status-and-next-steps.md`` §4o).

So the flow declines the attempt when the bond is carrying a session, and says
why. Three cases are pinned here, because getting any of them wrong turns a
UX fix into either a dead end or a lie:

1. **a live session** -> nothing is attempted, the entry is never unloaded, and
   the form re-shows with *forget the stale bond* as the way to insist;
2. **a bond that is not live** (the panel forgot us, the bus is deaf) -> the
   attempt runs exactly as before, because that is the only case a re-pair is
   for;
3. **a live session plus explicit consent** -> the attempt runs. The user's
   deliberate choice outranks our inference from a timestamp.

Part 2 — **an attempt this host cannot make must not be spent on the panel.**

Measured on the van 2026-10-27 (status file §4ak): every scanner was disabled --
the ESPHome entry that owns the proxy was off while the proxy's *bluetooth*
entry read "enabled", so Home Assistant had no scanner at all. Three submits
were each followed by ~90 s of `no connectable route` and then

  "Could not pair with the panel. Put it back into add-device mode and try
   again. If that fails, clear its saved Bluetooth device list ... power-cycle
   the panel ..."

-- instructions that would have destroyed a working bond, for a fault entirely
on the Pi. The state is decidable from the host (no connectable scanner is
registered at all), so the step now refuses *instantly* and names the host, and
keeps the panel-blame wording for the case that actually belongs to the panel:

* **no connectable scanner** -> ``no_scanner``, nothing attempted, entry not
  unloaded, and the text says the panel is not at fault;
* **scanners exist, nothing has heard the panel** -> ``panel_not_heard``, with
  the check-the-power/range advice instead of the list-clearing advice;
* **a route exists** -> the attempt runs, exactly as before (the positive
  control: a guard that silently blocks every pairing would pass the first two
  cases and fail this one).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

def _stub_voluptuous() -> None:
    """Enough of voluptuous for ``config_flow`` to build its schema bare.

    CI runs every test file that is not on its library list on an interpreter
    with nothing installed, and this one drives the real flow rather than a stub
    of it. So the *library* is stubbed here, only when it is genuinely absent --
    with the real one installed the test exercises the real schemas. What the
    assertions read is the field names the guard offers, which both shapes carry.
    """
    import types

    vol = types.ModuleType("voluptuous")

    class Schema:
        def __init__(self, schema, **_kw) -> None:
            self.schema = schema

        def __call__(self, value):
            return value

    class Marker:
        def __init__(self, schema, default=None, **_kw) -> None:
            self.schema = schema
            self.default = default

    class Invalid(Exception):
        pass

    # One update rather than nine assignments: a ModuleType has no declared
    # attributes, and the repo's other test fakes are built the same way.
    vol.__dict__.update(
        Schema=Schema,
        Optional=Marker,
        Required=Marker,
        Invalid=Invalid,
        In=lambda *a, **k: None,
        All=lambda *a, **k: None,
        Coerce=lambda *a, **k: None,
    )
    sys.modules["voluptuous"] = vol


try:  # the real thing when it is installed, a stand-in when it is not
    import voluptuous  # noqa: F401
except ModuleNotFoundError:
    _stub_voluptuous()


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

# What the host can actually reach, per test: a route object (or None), whether
# the panel is heard now, and which scanners can connect (None = cannot tell).
ROUTE: list[object | None] = [object()]
HEARD: list[bool] = [True]
SCANNERS: list[list[str] | None] = [["hci0"]]
WAITS: list[float] = []


async def _host_bond_state(_name, _address, _adapter_path=None):
    return HOST_BOND[0]


def _resolve_device(_hass, _name, **_kw):
    return ROUTE[0]


def _panel_advertising(_hass, _name, **_kw):
    return HEARD[0]


def _scanner_names(_hass):
    return SCANNERS[0]


async def _wait_until_heard(_hass, _name, **_kw):
    WAITS.append(1.0)
    return HEARD[0]


H._ConfigFlow.async_show_form = _show
CF.ensure_bonded = _ensure_bonded
CF.async_host_bond_state = _host_bond_state
# The preflight reads these; the defaults above describe a host that can pair,
# so the part-1 tests keep exercising the attempt itself.
CF.async_resolve_device = _resolve_device
CF.async_panel_advertising = _panel_advertising
CF.async_wait_until_heard = _wait_until_heard
CF.connectable_scanners = _scanner_names


def _submit(
    entry: _Entry,
    user_input: dict | None,
    host_bond: bool | None = None,
    *,
    route: object | None = "default",
    heard: bool = True,
    scanners: list[str] | None | str = "default",
) -> tuple[object, _Hass]:
    """Drive one submit of the reconfigure pair step."""
    CALLS.clear()
    SHOWN.clear()
    WAITS.clear()
    HOST_BOND[0] = host_bond
    ROUTE[0] = object() if route == "default" else route
    HEARD[0] = heard
    SCANNERS[0] = ["hci0"] if scanners == "default" else scanners
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


def test_a_host_with_no_scanner_refuses_at_once_and_clears_the_panel() -> None:
    """The measured 2026-10-27 case: no scanner at all, and the panel blamed.

    Three submits each spent ~90 s here and then told the user to clear the
    panel's device list and power-cycle it. Nothing can be attempted without a
    radio, the answer is decidable immediately, and the panel is not the broken
    half -- so: no attempt, no unload, no wait, and a message about the host.
    """
    shown, hass = _submit(
        _Entry(_Coordinator(live=False, last_ok=None)),
        {},
        route=None,
        heard=False,
        scanners=[],  # nothing connectable is registered
    )
    assert CALLS == [], f"the flow dialled with no scanner on the host: {CALLS}"
    assert hass.config_entries.unloaded == [], (
        "the entry was unloaded for an attempt that could not happen"
    )
    assert WAITS == [], (
        "the flow waited for an advert on a host that has no scanner to hear one"
    )
    assert SHOWN.get("errors", {}).get("base") == "no_scanner", SHOWN
    # Its own step: the pairing step's description tells the user to clear
    # the panel's list and power-cycle it, which is the opposite advice.
    assert getattr(shown, "step_id", None) == "pair_unreachable", SHOWN


def test_consent_cannot_spend_an_attempt_the_host_cannot_make() -> None:
    """Ticking *forget the stale bond* is consent, not a radio."""
    shown, _hass = _submit(
        _Entry(_Coordinator(live=False, last_ok=None)),
        {"forget_bond": True},
        route=None,
        heard=False,
        scanners=[],
    )
    assert CALLS == [], f"consent spent an impossible attempt: {CALLS}"
    assert SHOWN.get("errors", {}).get("base") == "no_scanner", SHOWN
    # Its own step: the pairing step's description tells the user to clear
    # the panel's list and power-cycle it, which is the opposite advice.
    assert getattr(shown, "step_id", None) == "pair_unreachable", SHOWN


def test_a_panel_nobody_hears_is_not_told_to_clear_its_list() -> None:
    """A scanner exists but hears nothing: that is power/range, not a stale bond.

    The list-clearing advice is destructive and belongs only to the case where
    the panel is answering and refusing us, so an unheard panel must not reach it.
    """
    shown, _hass = _submit(
        _Entry(_Coordinator(live=False, last_ok=None)),
        {},
        route=None,
        heard=False,
    )
    assert CALLS == [], f"the flow dialled a panel it cannot hear: {CALLS}"
    assert WAITS == [1.0], "the flow must give the panel a short window before refusing"
    assert SHOWN.get("errors", {}).get("base") == "panel_not_heard", SHOWN
    assert getattr(shown, "step_id", None) == "pair_unreachable", SHOWN


def test_a_reachable_panel_still_pairs() -> None:
    """The positive control: the guard must not block the case it is not about."""
    shown, hass = _submit(_Entry(_Coordinator(live=False, last_ok=None)), {})
    assert len(CALLS) == 1, f"a reachable panel was refused: {CALLS}"
    assert hass.config_entries.unloaded == ["E1"], "the entry must be unloaded: one dialler"



def _new_flow(entry: _Entry):
    """A flow wired to the fakes, for driving a step other than ``pair``."""
    flow = CF.TrumaConfigFlow()
    flow.source = "reconfigure"
    flow._name = PANEL
    flow._address = ADDR
    flow.hass = _Hass()
    flow._get_reconfigure_entry = lambda: entry
    return flow


def _call(flow, coro) -> tuple[object, str | None]:
    try:
        asyncio.run(coro)
    except H._Shown as shown:
        return shown, shown.step_id
    except H._Aborted as aborted:
        return aborted, None
    raise AssertionError("the step neither showed a form nor aborted")


def test_every_step_id_the_flow_returns_has_a_handler() -> None:
    """Home Assistant raises ``UnknownStep`` for a step_id with no handler.

    Measured on the van 2026-10-27: b53's refusal step reached a real browser as
    *"Unknown error occurred"*, because this suite mocks ``async_show_form`` and
    therefore never goes through Home Assistant's own step validation. The
    browser is the only place that defect appears, so this tripwire lives here:
    every step this file cares about must have its ``async_step_<id>`` method.
    """
    for step_id in ("user", "confirm", "pair", "pair_unreachable"):
        assert hasattr(CF.TrumaConfigFlow, f"async_step_{step_id}"), (
            f"step_id {step_id!r} is returned but has no async_step_{step_id}; "
            "Home Assistant will answer 'Unknown error occurred'"
        )


def test_the_refusal_screen_re_checks_instead_of_dead_ending() -> None:
    """Submit on the refusal must re-derive the answer, not repeat it forever."""
    ROUTE[0] = None
    HEARD[0] = False
    SCANNERS[0] = []
    flow = _new_flow(_Entry(_Coordinator(live=False, last_ok=None)))
    _shown, step = _call(flow, flow.async_step_pair_unreachable({}))
    assert step == "pair_unreachable", step
    assert SHOWN.get("errors", {}).get("base") == "no_scanner", SHOWN

    # The radio comes back and the panel is reachable: the same screen must now
    # lead into the pairing form rather than refusing again.
    ROUTE[0] = object()
    HEARD[0] = True
    SCANNERS[0] = ["hci0"]
    CALLS.clear()
    _shown, step = _call(flow, flow.async_step_pair_unreachable({}))
    assert step == "pair", step
    assert CALLS == [], "opening the pairing form must not attempt anything"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("reconfigure bond guard: all checks OK")


if __name__ == "__main__":
    main()
