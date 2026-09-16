#!/usr/bin/env python3
"""Offline checks for reading a diagnostics download back into a bus.

``bus.py`` needs no stubbing and no Home Assistant, which is why this is its
own file: ``tests/test_bus_dump_tool.py`` loads the integration's diagnostics
module and so needs the protocol stack behind it, and the reader tested here
is the half a person runs on somebody else's file.

Why it exists: ``from_diagnostics`` read one shape -- the value the
integration returns -- and a downloaded file is that value wrapped in
``data`` by Home Assistant. So ``tools/dump_bus.py somebody-else.json``
rebuilt an empty bus and printed nothing at all, for every real download,
including the three attached to issues. Nothing said it had failed.

What it pins:

1. a real 0.9 download, wrapper and all, reads back device by device,
2. a pre-0.9 download reads back too: its per-device store is where the
   devices come from, and addresses that answered but published nothing
   attributable are still on the bus,
3. a pre-0.7 download, which has only a flat store, lands in `unattributed`
   rather than under a device that would be a guess,
4. the integration's own return value keeps working, wrapper or not.

Run: ``python3 tests/test_diagnostics_shapes.py``
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"

_pkg = types.ModuleType("truma_pkg")
_pkg.__path__ = [str(SRC)]  # type: ignore[attr-defined]
sys.modules["truma_pkg"] = _pkg
_truma = types.ModuleType("truma_pkg.truma")
_truma.__path__ = [str(SRC / "truma")]  # type: ignore[attr-defined]
sys.modules["truma_pkg.truma"] = _truma
for _name, _path in (
    ("truma_pkg.truma.const", SRC / "truma" / "const.py"),
    ("truma_pkg.bus", SRC / "bus.py"),
):
    _spec = importlib.util.spec_from_file_location(_name, _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
BUS = sys.modules["truma_pkg.bus"]

PANEL = 0x0101
HEATER = 0x0201
BOARD = 0x0405


def _today() -> dict:
    """What this integration returns now, as a downloaded file holds it."""
    return {
        "home_assistant": {},
        "data": {
            "entry": {},
            "bus": {
                "assigned_addr": "0x0502",
                "devices": {
                    "0x0201": {
                        "addr": "0x0201",
                        "params": {"AirHeating.Temp": 228, "Identify.Name": "Combi"},
                        "param_meta": {"AirHeating.Temp": {"min": -400, "max": 600}},
                        "last_seen": 12.0,
                    }
                },
                "unattributed": {"System.Plugged": 1},
            },
        },
    }


def _pre_bus_model() -> dict:
    """A 0.8 download: a per-device store beside the flat one."""
    return {
        "home_assistant": {},
        "data": {
            "state": {
                "assigned_addr": 1282,
                "seen_devices": ["0x0101", "0x0201", "0x0405", "0x0601"],
                "device_params": {
                    "0x0405": {"FreshWater.Level": 50, "LinePower.Plugged": 0},
                    "0x0201": {"AirHeating.Temp": 198},
                },
                "device_param_meta": {
                    "0x0405": {"FreshWater.Level": {"min": 0, "max": 100}},
                },
                "raw_params": {
                    "FreshWater.Level": 50,
                    "AirHeating.Temp": 198,
                    "System.Plugged": 0,
                },
            }
        },
    }


def _pre_0_7() -> dict:
    """A 0.7 download: nothing in it says which device published what."""
    return {
        "home_assistant": {},
        "data": {
            "state": {
                "seen_devices": ["0x0101", "0x0201"],
                "raw_params": {"AirHeating.Temp": 224, "AirHeating.TgtTemp": 170},
            }
        },
    }


def test_a_real_download_is_read_wrapper_and_all() -> None:
    bus = BUS.from_diagnostics(_today())

    assert set(bus.devices) == {HEATER}, "a downloaded file read as an empty bus"
    heater = bus.device(HEATER)
    assert heater.params["AirHeating.Temp"] == 228
    assert heater.name == "Combi"
    assert heater.bounds("AirHeating", "Temp") == (-400, 600)
    assert bus.unattributed == {"System.Plugged": 1}
    assert bus.assigned_addr == 0x0502

    # The integration's own return value, unwrapped, still reads the same.
    assert BUS.from_diagnostics(_today()["data"]).devices.keys() == bus.devices.keys()


def test_a_pre_bus_model_download_is_read_too() -> None:
    bus = BUS.from_diagnostics(_pre_bus_model())

    # Every address that answered is on the bus, including the two that
    # published nothing attributable -- that they were there is half of what
    # a reader is looking for.
    assert set(bus.devices) == {PANEL, HEATER, BOARD, 0x0601}
    board = bus.device(BOARD)
    assert board.params["FreshWater.Level"] == 50
    assert board.bounds("FreshWater", "Level") == (0, 100)
    assert bus.device(HEATER).params == {"AirHeating.Temp": 198}
    # What the flat store held and no device claimed, and nothing else: the
    # two values above are not repeated as unattributed.
    assert bus.unattributed == {"System.Plugged": 0}
    assert bus.assigned_addr == 1282


def test_a_flat_download_is_not_assigned_to_a_guess() -> None:
    bus = BUS.from_diagnostics(_pre_0_7())

    assert set(bus.devices) == {PANEL, HEATER}
    assert all(not device.params for device in bus.devices.values()), (
        "a flat dump was filed under a device it never named"
    )
    assert bus.unattributed == {
        "AirHeating.Temp": 224,
        "AirHeating.TgtTemp": 170,
    }
    # ...and it still prints something rather than looking like an empty bus.
    assert "AirHeating.TgtTemp" in BUS.dump(bus)


def test_a_file_that_is_not_one_of_ours_reads_as_nothing() -> None:
    """The dump tool tells the user; the reader just comes back empty."""
    bus = BUS.from_diagnostics({"home_assistant": {}, "data": {"entry": {}}})
    assert not bus.devices and not bus.unattributed


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("diagnostics shapes: all checks OK")


if __name__ == "__main__":
    _main()
