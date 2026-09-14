#!/usr/bin/env python3
"""Offline checks for keeping the panel's own description of a parameter.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``coordinator._on_frame`` runs, and the frames it is fed are built and parsed
with the real ``truma.protocol``.

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
6. the values themselves still land under the device that published them,
7. the description is filed under that device too, and is what its own
   controls are built from,
8. and the result survives a diagnostics download, i.e. it is JSON.

Run: ``python3 tests/test_param_meta.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

HEATER = 0x0201
APP_ADDR = 0x0501

stubs.install_homeassistant()
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
COORD = stubs.load("coordinator")

import cbor2  # noqa: E402  - after the stubs, which put the package on sys.path


class _Coord:
    """Carries only what ``_on_frame`` touches."""

    hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=lambda: 0.0))
    unique_id = "Truma iNetX-FFB4D1"

    _client = None

    def __init__(self) -> None:
        self._bus = BUS.Bus()
        self._bus.assigned_addr = APP_ADDR
        self._last_frame = 0.0
        self.updates = 0

    def async_set_updated_data(self, _data) -> None:
        self.updates += 1

    _on_frame = COORD.TrumaCoordinator._on_frame


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

    meta = coord._bus.device(HEATER).meta("System", "FlameStatus")
    assert meta, "the device described the parameter and it was dropped"
    assert meta["enum"] == {"0": "Off", "1": "Gas", "2": "Electric"}, meta
    assert (meta["min"], meta["max"]) == (0, 2), meta
    # perm/avail say whether it can be written and whether it means anything on
    # this vehicle; both are part of the answer to "what is this".
    assert meta["perm"] == 1 and meta["avail"] == 1, meta


def test_the_value_still_lands_where_it_always_did() -> None:
    """Learning the description must not disturb the state it arrives with."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))

    assert coord._bus.device(HEATER).get("System", "FlameStatus") == 2
    assert coord.updates == 1, "the frame did not reach the entities"


def test_a_plain_update_describes_as_well_as_reports() -> None:
    """Info frames carry the same description; both paths have to keep it."""
    coord = _Coord()
    _feed(coord, 0x00, {"tn": "System", **FLAME_STATUS})

    assert coord._bus.device(HEATER).get("System", "FlameStatus") == 2
    assert coord._bus.device(HEATER).meta("System", "FlameStatus")["enum"]["1"] == "Gas"


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

    meta = coord._bus.device(HEATER).meta("EnergySrc", "DieselLevel")
    assert meta["enum"] == {"0": "Off", "1": "On"}, meta
    assert meta["enum_unavailable"] == ["1"], meta


def test_a_later_frame_that_says_less_erases_nothing() -> None:
    """Most frames are bare values. They must not undo the discovery answer."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))
    _feed(coord, 0x00, {"tn": "System", "pn": "FlameStatus", "v": 0})

    meta = coord._bus.device(HEATER).meta("System", "FlameStatus")
    assert meta["enum"] == {"0": "Off", "1": "Gas", "2": "Electric"}, meta
    assert coord._bus.device(HEATER).get("System", "FlameStatus") == 0


def test_a_parameter_with_no_value_is_still_described() -> None:
    """Unavailable is not nothing: it says the vehicle lacks the hardware."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "GasLevel",
        "avail": 0,
        "enum": [{"n": "Off", "a": True, "v": 0}, {"n": "On", "a": True, "v": 1}],
    }], topic="EnergySrc"))

    meta = coord._bus.device(HEATER).meta("EnergySrc", "GasLevel")
    assert meta["avail"] == 0, meta
    assert meta["enum"] == {"0": "Off", "1": "On"}, meta
    assert not coord._bus.device(HEATER).reports("EnergySrc", "GasLevel")


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
    assert coord._bus.device(HEATER).meta("System", "Weird") == {"max": 3}
    # A value with no description at all leaves no empty shell behind.
    assert not coord._bus.device(HEATER).meta("System", "Undescribed")


def test_the_description_survives_a_diagnostics_download() -> None:
    """It exists to be read by someone who was sent a download link."""
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([FLAME_STATUS]))

    dumped = json.loads(json.dumps(coord._bus.device(HEATER).param_meta))
    assert dumped["System.FlameStatus"]["enum"]["2"] == "Electric"


def test_the_description_is_what_a_control_is_built_from() -> None:
    """Kept so that entities stop guessing, not so a download reads better.

    ``number.py`` hard-coded 0-10 for the circulation fan, which is a Combi's
    range and was handed to every device that published the parameter. A
    device that states its own range gets its own slider.
    """
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "FanLevel", "v": 2, "perm": 1, "min": 1, "max": 6,
    }], topic="AirCirculation"))

    device = coord._bus.device(HEATER)
    assert device.bounds("AirCirculation", "FanLevel") == (1, 6)
    assert device.writable("AirCirculation", "FanLevel") is True
    bus = coord._bus
    assert bus.validate_write(HEATER, "AirCirculation", "FanLevel", 6)[0] is True
    ok, msg = bus.validate_write(HEATER, "AirCirculation", "FanLevel", 9)
    assert ok is False and "1-6" in msg, msg


def test_a_parameter_the_device_calls_read_only_refuses_a_write() -> None:
    """``perm`` is "permission, Integer" and nothing more in the reference.

    So 0 is read as a refusal on the strength of the field's name, and the
    message quotes the claim -- which is what gets a wrong reading of the
    field reported rather than silently losing somebody a control.
    """
    coord = _Coord()
    _feed(coord, TC.MBP_PARAM_DISC_RESP, _discovery([{
        "pn": "GasLevel", "v": 1, "perm": 0,
    }], topic="EnergySrc"))

    assert coord._bus.device(HEATER).writable("EnergySrc", "GasLevel") is False
    ok, msg = coord._bus.validate_write(HEATER, "EnergySrc", "GasLevel", 0)
    assert ok is False and "read-only" in msg, msg
    # A device that said nothing about permissions is not assumed to refuse.
    assert coord._bus.device(HEATER).writable("System", "FlameStatus") is None


def _main() -> None:
    stubs.run_tests(globals(), "parameter descriptions")


if __name__ == "__main__":
    _main()
