#!/usr/bin/env python3
"""Offline checks that two devices reporting one topic stay apart.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``coordinator._on_frame`` runs, and the frames it is fed are built and
parsed with the real ``truma.protocol``.

Why this exists (issue #9, measured on a Combi 6 E + iNet X Pro with a roof air
conditioner and two gas-bottle sensors): a topic is a message class, not a
device. Subscription names topics and is addressed to the broker, so every
device implementing a topic publishes under it, and the src address in the
frame header is the only thing telling two publishers apart. Stored flat under
``topic.param`` they overwrite each other -- and the result is not a visibly
stale reading but a plausible wrong one, assembled out of two devices:

    GasBtl.Name        "Rechts"
    GasBtl.FillLevelP  49        <- the *left* bottle; the right one is at 100

What it pins:

1. two fan levels under one parameter name stay two readings,
2. a record is not assembled out of two devices -- the name and the level
   under it come from the same sensor,
3. the panel's own identity is not overwritten by a device that describes
   itself under the same topic,
4. the message broker is not filed as a device,
5. the flat view every entity already reads is undisturbed,
6. and a diagnostics download names the devices in the form addresses are
   read and quoted in, not as decimal.

Run: ``python3 tests/test_device_params.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

APP_ADDR = 0x0501

# Addresses as measured on the reporter's vehicle. A device address is
# class << 8 | instance, which is exactly why two of a class collide.
PANEL = 0x0101
COMBI = 0x0201
ROOF_AC = 0x0406
BOTTLE_LEFT = 0x0603
BOTTLE_RIGHT = 0x0604
BROKER = 0x0000


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load():
    """Import the real coordinator, state, protocol and diagnostics."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod("homeassistant.components", __path__=[])
    # The real one drops keys; identity is enough to check the shape of what
    # is handed to it, and redaction is not what this test is about.
    _mod(
        "homeassistant.components.diagnostics",
        async_redact_data=lambda data, _keys: data,
    )
    _mod("homeassistant.helpers", __path__=[], issue_registry=types.SimpleNamespace(
        async_create_issue=lambda *a, **kw: None,
        async_delete_issue=lambda *a, **kw: None,
        IssueSeverity=types.SimpleNamespace(WARNING="warning"),
    ))
    _mod("homeassistant.helpers.storage", Store=object)
    _mod("bleak_retry_connector", BleakClientWithServiceCache=object,
         establish_connection=None)

    class _Coordinator:
        """DataUpdateCoordinator stand-in that tolerates [TrumaState]."""

        def __class_getitem__(cls, _item):
            return cls

    _mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])
    _mod("truma_pkg.ble", TrumaBleClient=object, device_from_bluez=None)
    _mod("truma_pkg.bt", async_panel_advertising=lambda *a: False,
         async_resolve_proxy_device=None, async_wait_until_heard=None)

    def _real(name: str, package: str = "truma_pkg", path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    truma_const = _real("const", "truma_pkg.truma", SRC / "truma")
    protocol = _real("protocol", "truma_pkg.truma", SRC / "truma")
    state = _real("state", "truma_pkg.truma", SRC / "truma")
    _real("const")
    coordinator = _real("coordinator")
    diagnostics = _real("diagnostics")
    return truma_const, protocol, state, coordinator, diagnostics


TC, PROTO, STATE, COORD, DIAG = _load()

import cbor2  # noqa: E402  - after _load(), which puts the package on sys.path


class _Coord:
    """Carries only what ``_on_frame`` touches."""

    hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=lambda: 0.0))
    unique_id = "Truma iNetX-FFB4D1"
    last_update_success = True

    def __init__(self) -> None:
        self._state = STATE.TrumaState()
        self._state.assigned_addr = APP_ADDR
        self._last_frame = 0.0
        self.updates = 0

    @property
    def data(self):
        return self._state

    def async_set_updated_data(self, _data) -> None:
        self.updates += 1

    _on_frame = COORD.TrumaCoordinator._on_frame
    _learn_param = COORD.TrumaCoordinator._learn_param


def _report(coord: _Coord, src: int, topic: str, param: str, value) -> None:
    """One device publishes one value, through the real frame path."""
    frame = PROTO.build_v3_frame(
        APP_ADDR, src, TC.CTRL_MBP, TC.MBP_INFO, 0,
        cbor2.dumps({"tn": topic, "pn": param, "v": value}),
    )
    parsed = PROTO.parse_v3_frame(frame)
    assert parsed is not None
    coord._on_frame(parsed)


def test_two_fans_under_one_parameter_name_stay_two_readings() -> None:
    """The whole of issue #9, as first reported.

    Combi at level 4, roof unit at level 2; the entity showed 2 because the
    roof unit spoke last.
    """
    coord = _Coord()
    _report(coord, COMBI, "AirCirculation", "FanLevel", 4)
    _report(coord, ROOF_AC, "AirCirculation", "FanLevel", 2)

    state = coord._state
    assert state.device_param(COMBI, "AirCirculation", "FanLevel") == 4
    assert state.device_param(ROOF_AC, "AirCirculation", "FanLevel") == 2
    # And the flat view is still the last writer, which is the behaviour the
    # entities keep until one of them asks for a device by name.
    assert state.raw_params["AirCirculation.FanLevel"] == 2


def test_a_record_is_not_assembled_out_of_two_devices() -> None:
    """The worse case: a name from one bottle, a level from the other.

    Measured over an hour on a stock build -- FillLevelP under the name
    "Rechts" read 100, then 49, then 52 -- which looks like a bottle being
    emptied rather than like two bottles taking turns in one slot.
    """
    coord = _Coord()
    _report(coord, BOTTLE_RIGHT, "GasBtl", "Name", "Rechts")
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)
    _report(coord, BOTTLE_LEFT, "GasBtl", "Name", "Links")
    _report(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", 49)

    state = coord._state
    for addr, name, level in (
        (BOTTLE_RIGHT, "Rechts", 100),
        (BOTTLE_LEFT, "Links", 49),
    ):
        assert state.device_param(addr, "GasBtl", "Name") == name
        assert state.device_param(addr, "GasBtl", "FillLevelP") == level, (
            "the level under this name came from the other bottle"
        )


def test_the_panel_keeps_its_own_identity() -> None:
    """Every device describes itself under Identify, so the flat one is a race.

    On the reporter's vehicle Identify.Name read "Truma LevelControl" and
    Identify.SerialNr a bottle sensor's, rather than the panel's.
    """
    coord = _Coord()
    _report(coord, PANEL, "Identify", "Name", "iNet X Pro Panel")
    _report(coord, BOTTLE_LEFT, "Identify", "Name", "Truma LevelControl")

    state = coord._state
    assert state.device_param(PANEL, "Identify", "Name") == "iNet X Pro Panel"
    assert state.device_param(BOTTLE_LEFT, "Identify", "Name") == "Truma LevelControl"
    # Unchanged, and this is why nothing may read the panel's identity from it.
    assert state.raw_params["Identify.Name"] == "Truma LevelControl"


def test_the_broker_is_not_a_device() -> None:
    """Address 0 is the message broker; it owns no parameters."""
    coord = _Coord()
    _report(coord, BROKER, "System", "FlameStatus", 1)

    assert BROKER not in coord._state.device_params
    # The value itself is not discarded -- only its attribution.
    assert coord._state.raw_params["System.FlameStatus"] == 1


def test_the_flat_view_is_undisturbed() -> None:
    """Entities read mapped fields off the flat view and must not notice."""
    coord = _Coord()
    _report(coord, COMBI, "AirCirculation", "FanLevel", 3)
    _report(coord, COMBI, "System", "FlameStatus", 2)

    assert coord._state.fan_level == 3
    assert coord._state.flame_status == 2
    assert coord._state.topic_source["AirCirculation"] == COMBI


def test_a_download_names_the_devices_the_way_they_are_quoted() -> None:
    """A dump is the evidence someone pastes into an issue; 0x0603, not 1539."""
    coord = _Coord()
    _report(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", 49)
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)

    entry = types.SimpleNamespace(
        runtime_data=coord, as_dict=lambda: {"title": "Truma"}
    )
    dumped = json.loads(json.dumps(
        asyncio.run(DIAG.async_get_config_entry_diagnostics(None, entry))
    ))

    devices = dumped["state"]["device_params"]
    assert set(devices) == {"0x0603", "0x0604"}, devices
    assert devices["0x0603"]["GasBtl.FillLevelP"] == 49
    assert devices["0x0604"]["GasBtl.FillLevelP"] == 100


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("per-device parameters: all checks OK")


if __name__ == "__main__":
    _main()
