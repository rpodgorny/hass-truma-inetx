#!/usr/bin/env python3
"""Offline check that a panel Truma renumbers is still offered for setup.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``bt`` helpers and the real ``config_flow`` steps run.

Why this exists: an iNet X Panel 2 was heard by its owner's proxy, reachable,
and never offered -- no discovery card, nothing in the UI to say why, which
from outside is indistinguishable from the integration not hearing it at all
(issue #6). Two separate gates did that. The config flow threw away any advert
whose name did not start with ``Truma iNetX``, even when a Truma service UUID
had routed it there; and the matching itself listed the two service UUIDs the
original panel puts on air, while a Panel 2 advertises a third, fc310006.

Both are now settled by an nRF Connect capture of a Panel 2 (hardware 1.4,
firmware 3.5.38.1). It advertises one service, fc310006, manufacturer data
under Truma's company ID 0x0c73 -- and the name ``Truma iNetX-5770EB``, which
does still carry the prefix. So the renamed-panel case is hypothetical rather
than live; it is kept below because nothing guarantees the next generation
keeps the prefix, and the first word of the UUID has already moved once. What
does not move is the UUID base, ``-f3b2-11e8-8eb2-f2801f1b9fd1``, so that is
what matching keys on.

Identification cannot simply drop the name test either, because Home Assistant
substitutes the (rotating) address when an advert carries no local name, which
is what passive scanning and the original panel's add-device mode both give.
Keying on that would spawn a new MAC-titled discovery card every RPA rotation.
So identification and keying are split: a Truma UUID *or* the known prefix
identifies a panel, and only a real name keys it.

What it pins:

1. the advert a real Panel 2 sends is identified and keyed by its own name,
2. any UUID in Truma's space identifies a panel, including numbers nobody has
   seen yet -- no release needed for the next renumbering,
3. a renamed panel carrying a Truma UUID is identified and keyed by the name it
   does advertise,
4. an advert whose "name" is really just the address never keys anything, in
   either flow step,
5. the original panel still works by name alone, with no service UUID in the
   advert (passive scanning shows no scan response),
6. an unrelated BLE device is not picked up by the manual step.

Run: ``python3 tests/test_panel2_discovery.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

# The panel this integration was written against.
PANEL_1 = "Truma iNetX-FFB4D1"
# What a Panel 2 actually calls itself, from the nRF Connect capture in issue
# #6: the prefix survived the hardware generation, the UUID did not.
PANEL_2 = "Truma iNetX-5770EB"
# A panel that drops the prefix. Hypothetical -- no such panel has been seen --
# and the point is that discovery must not depend on guessing this string.
PANEL_RENAMED = "Truma iNet X Panel 2-A1B2C3"

ADVERT_SERVICE_UUID = "fc310000-f3b2-11e8-8eb2-f2801f1b9fd1"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"
# What a Panel 2 advertises instead of either of the above (same capture).
PANEL2_ADVERT_SERVICE_UUID = "fc310006-f3b2-11e8-8eb2-f2801f1b9fd1"
# A number nobody has advertised yet, in Truma's space. Stands for the next
# renumbering: matching keys on the base, so this must work untouched.
UNSEEN_TRUMA_UUID = "fc31beef-f3b2-11e8-8eb2-f2801f1b9fd1"

RPA = "62:4A:BD:AD:73:5D"


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
        address: str = RPA,
    ) -> None:
        self.name = name
        self.service_uuids = list(uuids)
        self.address = address
        self.time = 0.0
        self.rssi = -70
        self.connectable = True


ADVERTS: list[_Info] = []


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


class _ConfigFlow:
    """The parts of homeassistant.config_entries.ConfigFlow the steps touch."""

    # Class attributes, not __init__: TrumaConfigFlow defines its own __init__
    # and does not chain up, exactly as it does under the real base class.
    hass = object()
    unique_id: str | None = None
    source = "bluetooth"

    def __init_subclass__(cls, **kwargs) -> None:  # domain=DOMAIN
        super().__init_subclass__()

    @property
    def context(self) -> dict:
        if not hasattr(self, "_context"):
            self._context: dict = {}
        return self._context

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        return None

    def _async_current_ids(self):
        return set()

    def _set_confirm_only(self):
        return None

    def async_abort(self, *, reason: str):
        raise _Aborted(reason)

    def async_show_form(self, *, step_id, data_schema=None, **kw):
        raise _Shown(step_id, data_schema)


def _load():
    """Import the real const/bt/config_flow with externals stubbed out."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod(
        "homeassistant.config_entries",
        SOURCE_RECONFIGURE="reconfigure",
        ConfigEntry=dict,
        ConfigFlow=_ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlow=object,
    )
    _mod("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
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
    )
    _mod("truma_pkg", __path__=[str(SRC)])
    # config_flow only reads two names off the coordinator, and pairing is
    # never reached by the steps under test.
    _mod("truma_pkg.coordinator", CONF_POLL_INTERVAL="poll", DEFAULT_POLL_INTERVAL=0)
    _mod("truma_pkg.pairing", ensure_bonded=None)

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


def _manual_choices(*infos: _Info) -> list[str]:
    """Names the manual (user) step would offer for these adverts."""
    ADVERTS[:] = infos
    flow = CF.TrumaConfigFlow()
    try:
        asyncio.run(flow.async_step_user())
    except _Aborted as abort:
        assert abort.reason == "no_devices_found", abort.reason
        return []
    except _Shown as shown:
        assert shown.step_id == "user"
    return sorted(flow._discovered)


def test_the_service_uuid_identifies_a_panel_on_its_own() -> None:
    assert BT.is_panel_advert(_Info(name=PANEL_RENAMED, uuids=(ADVERT_SERVICE_UUID,)))
    assert BT.is_panel_advert(_Info(name=PANEL_1))
    assert BT.is_panel_advert(_Info(uuids=(SERVICE_UUID,)))
    assert not BT.is_panel_advert(_Info(name="Some Sensor", uuids=("180f",)))


def test_the_panel_2_advert_is_recognised() -> None:
    """The captured Panel 2 advert: fc310006, which nothing matched before."""
    panel_2 = _Info(name=PANEL_2, uuids=(PANEL2_ADVERT_SERVICE_UUID,))
    assert BT.is_panel_advert(panel_2)
    assert BT.is_panel_advert(_Info(uuids=(PANEL2_ADVERT_SERVICE_UUID,)))
    result = _discover(panel_2)
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL_2
    assert _manual_choices(panel_2) == [PANEL_2]


def test_a_uuid_nobody_has_seen_yet_identifies_a_panel() -> None:
    """Matching keys on Truma's UUID base, so the next renumber costs nothing.

    Listing exact numbers is what hid a Panel 2 for weeks: it advertises
    fc310006, the list held fc310000 and fc310002, and nothing said so.
    """
    assert BT.is_panel_advert(_Info(uuids=(UNSEEN_TRUMA_UUID,)))
    assert BT.is_panel_advert(_Info(uuids=(UNSEEN_TRUMA_UUID.upper(),)))
    assert BT.is_panel_advert(_Info(name=PANEL_RENAMED, uuids=(UNSEEN_TRUMA_UUID,)))
    # Same first word, somebody else's base: not ours.
    assert not BT.is_panel_advert(
        _Info(uuids=("fc31beef-0000-1000-8000-00805f9b34fb",))
    )


def test_an_address_is_not_a_name() -> None:
    assert BT.advert_name(_Info(name=PANEL_2)) == PANEL_2
    # HA substitutes the address when the advert carries no local name.
    assert BT.advert_name(_Info(name=RPA, address=RPA)) is None
    assert BT.advert_name(_Info(name=RPA.lower(), address=RPA)) is None
    assert BT.advert_name(_Info(name="", address=RPA)) is None


def test_a_renamed_panel_is_discovered_and_keyed_by_its_own_name() -> None:
    """Half the regression in issue #6: this used to abort, showing no card.

    Hypothetical as of the Panel 2 capture, which kept the prefix -- but the
    name is Truma's to change, and nothing warns us when they do.
    """
    result = _discover(_Info(name=PANEL_RENAMED, uuids=(ADVERT_SERVICE_UUID,)))
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL_RENAMED
    assert result.context["title_placeholders"] == {"name": PANEL_RENAMED}


def test_the_original_panel_still_discovers_by_name_alone() -> None:
    result = _discover(_Info(name=PANEL_1))
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL_1


def test_a_nameless_pairing_advert_still_waits_for_a_name() -> None:
    """Keying on the rotating address would make a new card every rotation."""
    result = _discover(_Info(name=RPA, address=RPA, uuids=(ADVERT_SERVICE_UUID,)))
    assert isinstance(result, _Aborted)
    assert result.reason == "awaiting_name"


def test_the_manual_step_lists_a_renamed_panel() -> None:
    assert _manual_choices(_Info(name=PANEL_RENAMED, uuids=(ADVERT_SERVICE_UUID,))) == [
        PANEL_RENAMED
    ]
    assert _manual_choices(_Info(name=PANEL_1)) == [PANEL_1]
    assert _manual_choices(
        _Info(name=PANEL_1),
        _Info(name=PANEL_RENAMED, uuids=(SERVICE_UUID,), address="AA:BB:CC:DD:EE:FF"),
    ) == sorted((PANEL_1, PANEL_RENAMED))


def test_the_manual_step_skips_nameless_and_unrelated_adverts() -> None:
    assert _manual_choices(_Info(name=RPA, address=RPA, uuids=(SERVICE_UUID,))) == []
    assert _manual_choices(_Info(name="Some Sensor", uuids=("180f",))) == []


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("panel 2 discovery: all checks OK")


if __name__ == "__main__":
    main()
