#!/usr/bin/env python3
"""Offline checks for keeping the panel's own description of a parameter.

No hardware, no Home Assistant install: the HA/bleak imports are stubbed so the
real ``coordinator._on_frame`` runs, and the frames it is fed are built and
parsed with the real ``truma.protocol``.

Why this exists (issue #15, reported on a Combi 6 E + iNet X Pro):
``System.FlameStatus`` takes 0, 1 and 2 there, and the integration models it as
a binary sensor -- so whatever 2 means is silently folded into on or off. Truma
documents none of this. But the panel does: every value it sends is wrapped in
a description of the parameter carrying it -- ``min``, ``max``, whether it is
writable, and for an enum a list that *names each value*. The integration read
``pn`` and ``v`` out of that and threw the rest away, which is why the meaning
of a value has had to be measured on somebody's vehicle.

What it pins:

1. an enum the panel names is kept, per value, from a discovery answer,
2. and from a plain value update, which carries the same description,
3. a value the panel says this vehicle cannot produce is recorded as such --
   a heater with no diesel burner is still sent the enum entry that names it,
4. a later frame that describes less does not erase what is already known,
5. a parameter with no value is still described -- being unavailable is
   exactly the interesting case,
6. the values themselves still land where they always did,
7. and the result survives a diagnostics download, i.e. it is JSON.

Run: ``python3 tests/test_param_meta.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

HEATER = 0x0201
APP_ADDR = 0x0501


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load():
    """Import the real coordinator + truma protocol with externals stubbed."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
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
    return truma_const, protocol, state, coordinator


TC, PROTO, STATE, COORD = _load()

import cbor2  # noqa: E402  - after _load(), which puts the package on sys.path


class _Coord:
    """Carries only what ``_on_frame`` touches."""

    hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=lambda: 0.0))
    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self) -> None:
        self._state = STATE.TrumaState()
        self._state.assigned_addr = APP_ADDR
        self._last_frame = 0.0
        self.updates = 0

    def async_set_updated_data(self, _data) -> None:
        self.updates += 1

    _on_frame = COORD.TrumaCoordinator._on_frame
    _learn_param = COORD.TrumaCoordinator._learn_param


def _feed(coord: _Coord, sub_type: int, payload: dict) -> None:
    """Hand the coordinator a real frame, parsed back by the real parser."""
    frame = PROTO.build_v3_frame(
        APP_ADDR, HEATER, TC.CTRL_MBP, sub_type, 0, cbor2.dumps(payload)
    )
    parsed = PROTO.parse_v3_frame(frame)
    assert parsed is not None
    coord._on_frame(parsed)


def _discovery(parameters: list, topic: str = "System") -> dict:
    return {"topics": [{"tn": topic, "parameters": parameters}]}


# The shape of a described parameter, as the panel sends it: the value, its
# type and permissions, its range, and an enum naming every value.
FLAME_STATUS = {
    "pn": "FlameStatus",
    "v": 2,
    "type": 4,
    "perm": 1,
    "avail": 1,
    "min": 0,
    "max": 2,
    "enum": [
        {"n": "Off", "a": True, "v": 0},
        {"n": "Gas", "a": True, "v": 1},
        {"n": "Electric", "a": True, "v": 2},
    ],
}


def test_a_discovery_answer_teaches_the_names_of_a_tri_state() -> None:
    """The whole of issue #15: 0, 1 and 2 named by whoever defined them."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))

    meta = coord._state.param_meta.get("System.FlameStatus")
    assert meta is not None, "the panel described the parameter and it was dropped"
    assert meta["enum"] == {"0": "Off", "1": "Gas", "2": "Electric"}, meta
    assert (meta["min"], meta["max"]) == (0, 2), meta
    # perm/avail say whether it can be written and whether it means anything on
    # this vehicle; both are part of the answer to "what is this".
    assert meta["perm"] == 1 and meta["avail"] == 1, meta


def test_the_value_still_lands_where_it_always_did() -> None:
    """Learning the description must not disturb the state it arrives with."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))

    assert coord._state.flame_status == 2
    assert coord._state.raw_params["System.FlameStatus"] == 2
    assert coord.updates == 1, "the frame did not reach the entities"


def test_a_plain_update_describes_as_well_as_reports() -> None:
    """Info frames carry the same description; both paths have to keep it."""
    coord = _Coord()
    _feed(coord, 0x00, {"tn": "System", **FLAME_STATUS})

    assert coord._state.flame_status == 2
    assert coord._state.param_meta["System.FlameStatus"]["enum"]["1"] == "Gas"


def test_a_value_this_vehicle_cannot_produce_is_marked() -> None:
    """A Combi 6 E has no diesel burner, and its panel still names the value.

    Which values are *available* is the difference between "this enum exists"
    and "this vehicle can show it" -- and it is per-installation, so it is
    exactly what a diagnostics download needs to say.
    """
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "DieselLevel",
        "v": 0,
        "enum": [
            {"n": "Off", "a": True, "v": 0},
            {"n": "On", "a": False, "v": 1},
        ],
    }], topic="EnergySrc"))

    meta = coord._state.param_meta["EnergySrc.DieselLevel"]
    assert meta["enum"] == {"0": "Off", "1": "On"}, meta
    assert meta["enum_unavailable"] == ["1"], meta


def test_a_later_frame_that_says_less_erases_nothing() -> None:
    """Most frames are bare values. They must not undo the discovery answer."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))
    _feed(coord, 0x00, {"tn": "System", "pn": "FlameStatus", "v": 0})

    meta = coord._state.param_meta["System.FlameStatus"]
    assert meta["enum"] == {"0": "Off", "1": "Gas", "2": "Electric"}, meta
    assert coord._state.flame_status == 0


def test_a_parameter_with_no_value_is_still_described() -> None:
    """Unavailable is not nothing: it says the vehicle lacks the hardware."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "GasLevel",
        "avail": 0,
        "enum": [{"n": "Off", "a": True, "v": 0}, {"n": "On", "a": True, "v": 1}],
    }], topic="EnergySrc"))

    meta = coord._state.param_meta["EnergySrc.GasLevel"]
    assert meta["avail"] == 0, meta
    assert meta["enum"] == {"0": "Off", "1": "On"}, meta
    assert "EnergySrc.GasLevel" not in coord._state.raw_params


def test_junk_is_ignored_rather_than_stored() -> None:
    """A malformed enum must not make the description unreadable."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "Weird",
        "v": 1,
        "max": 3,
        "enum": ["not a dict", {"n": "Named", "v": "not an int"}, {"v": 3}],
    }, {
        "pn": "Undescribed",
        "v": 1,
    }]))

    # The range it did state is kept; nothing in that enum names a value.
    assert coord._state.param_meta["System.Weird"] == {"max": 3}
    # A value with no description at all leaves no empty shell behind.
    assert "System.Undescribed" not in coord._state.param_meta


def test_the_description_survives_a_diagnostics_download() -> None:
    """It exists to be read by someone who was sent a download link."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))

    dumped = json.loads(json.dumps(coord._state.param_meta))
    assert dumped["System.FlameStatus"]["enum"]["2"] == "Electric"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("parameter descriptions: all checks OK")


if __name__ == "__main__":
    _main()
