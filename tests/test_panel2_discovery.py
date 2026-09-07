#!/usr/bin/env python3
"""Offline check that a panel Truma renames is still offered for setup.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``bt`` helpers and the real ``config_flow`` steps run.

Why this exists: the config flow identified a panel by its advertised local
name, ``Truma iNetX...``. The manifest also matches on two Truma-proprietary
service UUIDs, and those matches route to ``async_step_bluetooth`` -- which then
threw the advert away because the name did not start with the expected prefix.
A panel whose name Truma changes is therefore heard, reachable and never
offered, with no discovery card and nothing in the UI to say why. The iNet X
Panel 2 in issue #6 is the live case; the reporter's panel is visible to the
proxy and invisible to the integration.

The fix cannot simply drop the name test, because Home Assistant substitutes
the (rotating) address when an advert carries no local name, and the panel's
add-device adverts carry none. Keying on that would spawn a new MAC-titled
discovery card every RPA rotation. So identification and keying were split:
service UUID *or* known prefix identifies a panel, a real name keys it.

What it pins:

1. a renamed panel carrying the service UUID is identified and keyed by the
   name it does advertise,
2. an advert whose "name" is really just the address never keys anything, in
   either flow step,
3. the original panel still works by name alone, with no service UUID in the
   advert (passive scanning shows no scan response),
4. an unrelated BLE device is not picked up by the manual step.

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
# A plausible Panel 2 advert: same proprietary service UUID, a name that no
# longer starts with the prefix above. The exact string is a guess -- the point
# of the test is that discovery must not depend on guessing it right.
PANEL_2 = "Truma iNet X Panel 2-A1B2C3"

ADVERT_SERVICE_UUID = "fc310000-f3b2-11e8-8eb2-f2801f1b9fd1"
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"

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
    _mod("truma_pkg.truma", __path__=[])
    _mod("truma_pkg.truma.const", SERVICE_UUID=SERVICE_UUID)
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
    assert BT.is_panel_advert(_Info(name=PANEL_2, uuids=(ADVERT_SERVICE_UUID,)))
    assert BT.is_panel_advert(_Info(name=PANEL_1))
    assert BT.is_panel_advert(_Info(uuids=(SERVICE_UUID,)))
    assert not BT.is_panel_advert(_Info(name="Some Sensor", uuids=("180f",)))


def test_an_address_is_not_a_name() -> None:
    assert BT.advert_name(_Info(name=PANEL_2)) == PANEL_2
    # HA substitutes the address when the advert carries no local name.
    assert BT.advert_name(_Info(name=RPA, address=RPA)) is None
    assert BT.advert_name(_Info(name=RPA.lower(), address=RPA)) is None
    assert BT.advert_name(_Info(name="", address=RPA)) is None


def test_a_renamed_panel_is_discovered_and_keyed_by_its_own_name() -> None:
    """The regression from issue #6: this used to abort, showing no card."""
    result = _discover(_Info(name=PANEL_2, uuids=(ADVERT_SERVICE_UUID,)))
    assert not isinstance(result, _Aborted), f"aborted: {result}"
    assert result.unique_id == PANEL_2
    assert result.context["title_placeholders"] == {"name": PANEL_2}


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
    assert _manual_choices(_Info(name=PANEL_2, uuids=(ADVERT_SERVICE_UUID,))) == [
        PANEL_2
    ]
    assert _manual_choices(_Info(name=PANEL_1)) == [PANEL_1]
    assert _manual_choices(
        _Info(name=PANEL_1),
        _Info(name=PANEL_2, uuids=(SERVICE_UUID,), address="AA:BB:CC:DD:EE:FF"),
    ) == sorted((PANEL_1, PANEL_2))


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
