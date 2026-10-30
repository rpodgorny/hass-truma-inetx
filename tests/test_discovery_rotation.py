#!/usr/bin/env python3
"""Offline check that a rotating address does not cost the user the card.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``config_flow`` discovery steps run.

Why this exists: the panel advertises a rotating (resolvable private) address,
so every rotation arrives at ``async_step_bluetooth`` as a **new** flow carrying
the same stable name. Two things then went wrong, and neither is visible in a
log:

1. The older card already holds ``unique_id = <name>``, and
   ``async_set_unique_id`` raises ``already_in_progress`` by default -- so the
   *new* flow died. Core had already recorded the match for the new address by
   then (``BluetoothMatcherIndex.match_domains`` fires once per address until
   that address disappears), so that address's one discovery trigger was spent
   on a flow that aborted immediately.
2. The older card went on pointing at an address that was no longer on air.
   When it expired -- 195 s of silence plus up to 300 s until the manager's
   unavailable tick, ``home-assistant/ble-scan-and-discovery.md`` §4a -- core
   dismissed the card, and because of (1) nothing replaced it until the panel
   rotated again, up to another quarter of an hour.

And a card that *does* survive a rotation (core protects a flow the user has
pressed Submit on) dials the address captured when the card was created, which
is the stale one.

What it pins:

1. a rotation hands the card over to the new flow instead of aborting it, and
   the older flow is aborted -- one panel, one card, always the live address,
2. a card the user has pressed Submit on is never taken away,
3. neither is a manual "Add integration" flow, which is a user mid-choice,
4. with no other flow in progress nothing is aborted and the card is shown,
5. Submit dials the address the panel is advertising **now**, not the card's,
6. but a panel still on air under the card's address is left alone -- the
   second (phantom) RPA of a just-paired panel must not win on freshness,
7. and when nothing is heard at all the stored address is kept: the pairing
   step resolves by name anyway, so guessing would only add a failure mode.

Run: ``python3 tests/test_discovery_rotation.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

NAME = "Truma iNetX-FFB4D1"
# Two addresses of the same panel, one rotation apart.
OLD_RPA = "62:4A:BD:AD:73:5D"
NEW_RPA = "74:46:81:EF:4A:63"
# A third, for the "both on air at once" case: this panel advertises a phantom
# RPA beside the live one after a bond, and freshness alone must not prefer it.
PHANTOM_RPA = "4F:F4:6F:B6:E8:4D"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"

ADVERTS: list = []
SIBLINGS: list = []
ABORTS: list[str] = []


class _Aborted(Exception):
    """Raised in place of HA's abort, so a step's outcome is unambiguous."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Shown(Exception):
    """Raised in place of HA's async_show_form."""

    def __init__(self, step_id: str, schema=None) -> None:
        super().__init__(step_id)
        self.step_id = step_id
        self.schema = schema


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
        address: str = OLD_RPA,
        uuids: tuple[str, ...] = (SERVICE_UUID,),
        time: float = 0.0,
    ) -> None:
        self.name = name
        self.service_uuids = list(uuids)
        self.address = address
        self.time = time
        self.rssi = -70
        self.connectable = True


class _Sibling:
    """An in-progress flow as ``FlowManager.async_progress_by_handler`` words it."""

    def __init__(
        self,
        flow_id: str = "older-flow",
        unique_id: str | None = NAME,
        address: str = OLD_RPA,
        source: str = "bluetooth",
        protected: bool = False,
    ) -> None:
        self.flow_id = flow_id
        self.unique_id = unique_id
        self.address = address
        self.source = source
        self.protected = protected

    def result(self) -> dict:
        context: dict = {"source": self.source}
        if self.unique_id is not None:
            context["unique_id"] = self.unique_id
        if self.protected:
            context["dismiss_protected"] = True
        return {
            "flow_id": self.flow_id,
            "handler": "truma_inetx",
            "context": context,
            "step_id": "confirm",
            "init_data": _Info(name=NAME, address=self.address),
        }


class _FlowManager:
    """The only flow-manager call the takeover makes."""

    def async_abort(self, flow_id: str) -> None:
        ABORTS.append(flow_id)


class _ConfigEntries:
    flow = _FlowManager()


class _Hass:
    """The parts of HomeAssistant the discovery steps touch."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.background: list[str] = []
        self.config_entries = _ConfigEntries()

    def async_create_background_task(self, coro, name, eager_start=True):
        self.background.append(name)
        coro.close()


class _ConfigFlow:
    """The parts of homeassistant.config_entries.ConfigFlow the steps touch."""

    hass = _Hass()
    unique_id: str | None = None
    source = "bluetooth"
    flow_id = "new-flow"

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__()

    @property
    def context(self) -> dict:
        if not hasattr(self, "_context"):
            self._context: dict = {}
        return self._context

    def _async_in_progress(self, include_uninitialized=False, match_context=None):
        """Faithful enough: other flows for this handler, minus this one."""
        out = []
        for sibling in SIBLINGS:
            result = sibling.result()
            if result["flow_id"] == self.flow_id:
                continue
            if match_context and not dict(match_context).items() <= result[
                "context"
            ].items():
                continue
            out.append(result)
        return out

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        if raise_on_progress and self._async_in_progress(
            include_uninitialized=True, match_context={"unique_id": unique_id}
        ):
            raise _Aborted("already_in_progress")
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        return None

    def _async_current_ids(self):
        return set()

    def _set_confirm_only(self):
        return None

    def async_abort(self, *, reason: str, **kwargs):
        raise _Aborted(reason)

    def async_show_form(self, *, step_id, data_schema=None, **kw):
        raise _Shown(step_id, data_schema)


async def _nothing_remembered(_address):
    """A BlueZ with no stored device for this address."""
    return None


async def _no_new_adverts(_hass, _duration=None) -> None:
    """A sweep that finds nothing new."""


def _load():
    """Import the real const/bt/config_flow with externals stubbed out."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod(
        "homeassistant.config_entries",
        SOURCE_RECONFIGURE="reconfigure",
        SOURCE_BLUETOOTH="bluetooth",
        ConfigEntry=dict,
        ConfigEntryState=type(
            "ConfigEntryState", (), {"LOADED": "loaded", "NOT_LOADED": "not_loaded"}
        ),
        ConfigFlow=_ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlow=object,
    )
    _mod("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
    # ``config_flow`` builds its schemas with voluptuous, and only the steps
    # that render a form need it. Stubbed rather than installed so this file
    # keeps running on a bare interpreter, like the rest of the suite; the
    # cards these checks do render are confirm-only, with no schema at all.
    _mod(
        "voluptuous",
        Schema=lambda *a, **kw: None,
        Required=lambda *a, **kw: a[0] if a else None,
        Optional=lambda *a, **kw: a[0] if a else None,
        In=lambda *a, **kw: a[0] if a else None,
    )
    _mod("homeassistant.components", __path__=[])
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=object)
    _mod(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=_Info,
        async_ble_device_from_address=lambda *a, **kw: None,
        async_discovered_service_info=lambda _hass, connectable=True: list(ADVERTS),
        async_scanner_devices_by_address=lambda *a, **kw: [],
        async_request_active_scan=_no_new_adverts,
    )

    class _IssueSeverity:
        WARNING = "warning"

    def _create_issue(_hass, domain, issue_id, **kw):
        return None

    def _delete_issue(_hass, domain, issue_id):
        return None

    helpers = _mod("homeassistant.helpers", __path__=[])
    registry = _mod(
        "homeassistant.helpers.issue_registry",
        async_create_issue=_create_issue,
        async_delete_issue=_delete_issue,
        IssueSeverity=_IssueSeverity,
    )
    helpers.issue_registry = registry

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.coordinator", CONF_POLL_INTERVAL="poll", DEFAULT_POLL_INTERVAL=0)
    _mod("truma_pkg.ble", device_from_bluez=_nothing_remembered)
    # Both names config_flow imports; the discovery steps never reach either.
    _mod("truma_pkg.pairing", ensure_bonded=None, async_host_bond_state=None)

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
    config_flow = _real("config_flow")
    return const, bt, config_flow


CONST, BT, CF = _load()
BT.SWEEP_DEADLINE = 0.0
BT.SWEEP_RETRY_PAUSE = 0.0


def _reset(*, adverts=(), siblings=()) -> None:
    ADVERTS[:] = list(adverts)
    SIBLINGS[:] = list(siblings)
    ABORTS.clear()


def _discover(info: _Info):
    """Run async_step_bluetooth on one advert; return the flow or the abort."""
    flow = CF.TrumaConfigFlow()
    try:
        asyncio.run(flow.async_step_bluetooth(info))
    except _Aborted as abort:
        return abort
    except _Shown:
        pass
    return flow


# --------------------------------------------------------------------------
# 1-4: who owns the card when the address rotates
# --------------------------------------------------------------------------


def test_a_rotation_hands_the_card_to_the_new_flow() -> None:
    """The new advert wins, and the older card is retired -- not the new flow."""
    _reset(
        adverts=[_Info(name=NAME, address=NEW_RPA, time=100.0)],
        siblings=[_Sibling(flow_id="older-flow", address=OLD_RPA)],
    )
    result = _discover(_Info(name=NAME, address=NEW_RPA, time=100.0))
    assert not isinstance(result, _Aborted), (
        "the rotation aborted the fresh flow, which is the defect: "
        f"{result.reason}"
    )
    assert ABORTS == ["older-flow"], ABORTS
    assert result.unique_id == NAME
    # The flow must proceed to the confirmation card, on the new address.
    assert result._discovery_info.address == NEW_RPA


def test_a_card_the_user_submitted_is_never_taken_away() -> None:
    """``dismiss_protected`` is core's rule; this flow obeys it too."""
    _reset(
        adverts=[_Info(name=NAME, address=NEW_RPA, time=100.0)],
        siblings=[_Sibling(flow_id="older-flow", address=OLD_RPA, protected=True)],
    )
    result = _discover(_Info(name=NAME, address=NEW_RPA, time=100.0))
    assert isinstance(result, _Aborted), "a protected card was replaced mid-attempt"
    assert result.reason == "card_in_progress", result.reason
    assert ABORTS == [], ABORTS


def test_a_manual_flow_keeps_the_panel() -> None:
    """A user picking the panel by hand is not a stale card to be retired."""
    _reset(
        adverts=[_Info(name=NAME, address=NEW_RPA, time=100.0)],
        siblings=[_Sibling(flow_id="user-flow", address=OLD_RPA, source="user")],
    )
    result = _discover(_Info(name=NAME, address=NEW_RPA, time=100.0))
    assert isinstance(result, _Aborted), "a manual flow's panel was taken away"
    assert result.reason == "card_in_progress", result.reason
    assert ABORTS == [], ABORTS


def test_a_lone_advert_still_shows_one_card() -> None:
    """No other flow: nothing is aborted and the card is shown as before."""
    _reset(adverts=[_Info(name=NAME, address=OLD_RPA)])
    result = _discover(_Info(name=NAME, address=OLD_RPA))
    assert not isinstance(result, _Aborted), getattr(result, "reason", result)
    assert ABORTS == [], ABORTS
    assert result.unique_id == NAME


# --------------------------------------------------------------------------
# 5-7: what Submit dials
# --------------------------------------------------------------------------


def _submitting_flow(card_address: str):
    """A flow sitting on the confirmation card, plus the address it will dial."""
    flow = CF.TrumaConfigFlow()
    flow._discovery_info = _Info(name=NAME, address=card_address)
    flow._name = NAME
    flow._address = None
    dialled: list[str] = []

    async def _pair(user_input=None):
        dialled.append(flow._address)
        raise _Aborted("pair-reached")

    flow.async_step_pair = _pair  # type: ignore[method-assign]
    return flow, dialled


def test_submit_dials_the_address_that_is_on_air() -> None:
    """After a rotation the card's address is dead; Submit must not dial it."""
    _reset(adverts=[_Info(name=NAME, address=NEW_RPA, time=500.0)])
    flow, dialled = _submitting_flow(OLD_RPA)
    try:
        asyncio.run(flow.async_step_confirm({}))
    except _Aborted as abort:
        assert abort.reason == "pair-reached", abort.reason
    assert dialled == [NEW_RPA], dialled


def test_submit_leaves_a_live_address_alone() -> None:
    """Both RPAs on air is the post-bond phantom case: no freshness contest."""
    _reset(
        adverts=[
            _Info(name=NAME, address=OLD_RPA, time=100.0),
            _Info(name=NAME, address=PHANTOM_RPA, time=900.0),
        ]
    )
    flow, dialled = _submitting_flow(OLD_RPA)
    try:
        asyncio.run(flow.async_step_confirm({}))
    except _Aborted as abort:
        assert abort.reason == "pair-reached", abort.reason
    assert dialled == [OLD_RPA], dialled


def test_submit_keeps_the_stored_address_when_nothing_is_heard() -> None:
    """Silence is not evidence of a rotation: keep what we have."""
    _reset(adverts=[])
    flow, dialled = _submitting_flow(OLD_RPA)
    try:
        asyncio.run(flow.async_step_confirm({}))
    except _Aborted as abort:
        assert abort.reason == "pair-reached", abort.reason
    assert dialled == [OLD_RPA], dialled


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("discovery rotation: all checks OK")


if __name__ == "__main__":
    main()
