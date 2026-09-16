#!/usr/bin/env python3
"""Offline check that a diagnostics download still reads back into a dump.

``tools/dump_bus.py`` is the only way to look at somebody's bus without Home
Assistant, and it earns that by not importing Home Assistant at all: it puts
``truma_inetx`` on ``sys.modules`` by hand, pointing at the source directory,
and loads ``bus`` out of it without ever running the package ``__init__`` --
which is the integration's Home Assistant entry point and would fail on its
first line.

Neither end of that is covered by the tests next door. They exercise ``bus``
by importing it the same way themselves, so the launcher could stop working --
a moved file, a renamed module, a package ``__init__`` that grows an import --
and nothing would say so until somebody with a broken vehicle was asked for a
dump and could not produce one. And the download's own shape is a contract
between two files that never meet: ``diagnostics.py`` writes it inside Home
Assistant, ``bus.from_diagnostics`` reads it outside.

So the download here is written by the real ``diagnostics.py`` rather than by
hand -- a hand-written one drifts, and silently, because the reader fills in
what it does not find -- and the real script then reads it in its own process,
the way a person would.

What it pins:

1. what diagnostics.py writes is what from_diagnostics reads: devices with
   their names, serials and parameter descriptions, not a flat list,
2. the launcher loads and exits cleanly on it,
3. and a structured value survives the round trip, printed as it arrived,
   because a dump is for seeing what the panel really sent.

Run: ``python3 tests/test_bus_dump_tool.py``
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "dump_bus.py"

PANEL = 0x0101
COMBI = 0x0201
# The panel's own Bluetooth side, which names itself nothing.
BLE_MGMT = 0x0601

stubs.install_homeassistant()
stubs.stub_transport()
BUS = stubs.load("bus")
stubs.load("const")
stubs.load("coordinator")
DIAG = stubs.load("diagnostics")


def _download() -> dict:
    """A download of the van's bus, written by the real diagnostics code."""
    bus = BUS.Bus()
    bus.assigned_addr = 0x0501

    def report(addr: int, topic: str, param: str, value, **meta) -> None:
        bus.learn_param(topic, param, {"v": value, **meta}, addr)
        bus.update(topic, param, value, addr)

    report(PANEL, "Identify", "Name", "iNet X Panel")
    report(PANEL, "Identify", "SerialNr", "23456789")
    report(PANEL, "System", "FlameStatus", 1, type=105, perm=0, avail=1)
    report(COMBI, "Identify", "Name", "Combi D 4 GEN2")
    report(COMBI, "WaterHeating", "Temp", 401, type=1, perm=0, avail=1)
    # Measured on the van: a count per kind of device, not a count.
    report(
        BLE_MGMT,
        "BleDeviceManagement",
        "NrFreeSlots",
        [{"type": 12, "nrOfSlots": 1}, {"type": 9, "nrOfSlots": 2}],
        type=203,
        perm=0,
        avail=1,
    )
    # A value the frame named no device for; the dump lists these separately.
    bus.unattributed["System.Search"] = 0

    coordinator = types.SimpleNamespace(
        data=bus,
        unique_id="Truma iNetX-FFB4D1",
        last_update_success=True,
        address_kind="identity",
        session_transport="local",
        _client=None,
        poll_interval=0,
    )
    entry = types.SimpleNamespace(
        runtime_data=coordinator, as_dict=lambda: {"title": "Truma"}
    )
    return asyncio.run(DIAG.async_get_config_entry_diagnostics(None, entry))


def test_a_download_reads_back_into_a_dump() -> None:
    """The whole tool, in one process, on what a user would send in."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "diagnostics.json"
        path.write_text(json.dumps(_download()))
        done = subprocess.run(
            [sys.executable, str(TOOL), str(path)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    assert done.returncode == 0, done.stderr
    out = done.stdout

    # Devices, named by the bus, each with its own parameters under it.
    assert "0x0101" in out and "iNet X Panel" in out, out
    assert "23456789" in out, out
    assert "0x0201" in out and "Combi D 4 GEN2" in out, out
    # A device that names itself nothing is still a device here: the dump is
    # the bus as it arrived, not as Home Assistant presents it.
    assert "0x0601" in out and "unnamed" in out, out

    # The panel's own description of a parameter comes through with it.
    assert "System.FlameStatus" in out and "avail 1" in out, out

    # Printed as it arrived. The sensor reduces this one to a count; the dump
    # must not, or the one place that shows what the panel really sent stops
    # showing it.
    assert "'nrOfSlots': 1" in out and "'nrOfSlots': 2" in out, out

    # And the two sections that exist only because the bus is per-device.
    assert "no flat reading" in out, out
    assert "System.Search" in out, out


def _main() -> None:
    stubs.run_tests(globals(), "bus dump tool")


if __name__ == "__main__":
    _main()
