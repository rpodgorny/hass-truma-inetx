#!/usr/bin/env python3
"""Offline checks that a diagnostics download carries no Bluetooth address.

The download is what people paste into issues and attach to threads, so every
address in one is published for good. Two rounds of this have now been paid
for: the config entry's own address, which redaction by key name missed inside
a ``DiscoveryKey`` repr until b15, and then #35 -- the gas-bottle sensors
publish *their* addresses as ordinary bus parameters, which travel a path
nothing redacted at all.

    "0x0603": {"params": {"BluetoothDevice.BleAddress": "90:7B:C6:..",
                          "BluetoothDevice.BleAddressType": 0, ...

``BleAddressType: 0`` says those are public addresses: permanent identifiers
rather than rotating ones, and one of the two shared a prefix with that
panel's identity address.

Why this file rather than the two next door: ``test_diagnostics_shapes.py``
runs ``bus.py`` with no Home Assistant at all and cannot see the writer, and
``test_bus_dump_tool.py``, which does drive the real writer, needs cbor2 for
the protocol stack behind it. Redaction needs neither. It is the one thing in
a download that has to hold whether or not somebody installed a library, so it
is checked where nothing can be skipped.

What it pins:

1. an address a *device* published is gone, wherever in the bus it sits,
2. every spelling is caught, including the panel's advertised name, and
   including values no key name could have found,
3. what a reader actually needs -- serials, labels, readings, the address
   *type* -- survives the pass,
4. and the redacted download still reads back into a bus.

Run: ``python3 tests/test_diagnostics_redaction.py``
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = 0x0101
HEATER = 0x0201
# The two LevelControl gas-bottle sensors of #35, on the vehicle that reported
# it. Their addresses here are invented: the point is that whatever they are,
# they do not reach the file.
BOTTLE_RIGHT = 0x0603
BOTTLE_LEFT = 0x0604

RIGHT_ADDR = "90:7B:C6:1D:44:0E"
LEFT_ADDR = "74:46:B3:2A:91:C7"

stubs.install_homeassistant()
BUS = stubs.load("bus")
# The real diagnostics module imports the coordinator only for a type; the
# coordinator brings in the protocol stack, which needs cbor2.
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
DIAG = stubs.load("diagnostics")

# The same shapes the writer hunts, spelled out again here rather than imported
# from it: a test that reuses the pattern under test passes when both are wrong
# together. This is the one in tests/test_diagnostics_shapes.py, which is what
# guards the committed dumps.
LEAK = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b|iNetX-[0-9A-Fa-f]{6}")


def _bus() -> object:
    """A bus with two bottle sensors that publish their own addresses."""
    bus = BUS.Bus()
    bus.assigned_addr = 0x0501

    def report(addr: int, topic: str, param: str, value, **meta) -> None:
        bus.learn_param(topic, param, {"v": value, **meta}, addr)
        bus.update(topic, param, value, addr)

    report(PANEL, "Identify", "Name", "iNet X Panel")
    report(PANEL, "Identify", "SerialNr", "IPBMTEU-A-34317973")
    report(HEATER, "Identify", "Name", "Combi 6 E")
    report(HEATER, "Identify", "SerialNr", "CBG04EU-E-35008040")

    for addr, label, ble in (
        (BOTTLE_RIGHT, "Rechts", RIGHT_ADDR),
        (BOTTLE_LEFT, "Links", LEFT_ADDR),
    ):
        report(addr, "Identify", "Name", "Truma LevelControl")
        report(addr, "Identify", "SerialNr", f"LC-{label}")
        report(addr, "GasBtl", "Name", label)
        report(addr, "GasBtl", "FillLevelP", 100, type=1, perm=0, avail=1)
        # Described as a string like any other, which is the whole reason this
        # got out: nothing about the description says it is an identifier.
        report(addr, "BluetoothDevice", "BleAddress", ble, type=4, avail=1)
        report(addr, "BluetoothDevice", "BleAddressType", 0, type=2, avail=1)
    return bus


def _download(bus, entry_dict: dict | None = None, address: str | None = None) -> dict:
    """What the real diagnostics writer returns for that bus."""
    coordinator = types.SimpleNamespace(
        data=bus,
        unique_id="Truma iNetX-FFB4D1",
        last_update_success=True,
        address_kind="identity",
        session_transport="local",
    )
    if address is not None:
        coordinator.address = address
    entry = types.SimpleNamespace(
        runtime_data=coordinator,
        as_dict=lambda: entry_dict if entry_dict is not None else {"title": "Truma"},
    )
    return asyncio.run(DIAG.async_get_config_entry_diagnostics(None, entry))


def test_an_address_a_device_published_is_gone() -> None:
    download = _download(_bus())
    blob = json.dumps(download)

    assert RIGHT_ADDR not in blob, "a bottle sensor's own address is in the download"
    assert LEFT_ADDR not in blob, "a bottle sensor's own address is in the download"
    # Not "the two we knew about": nothing address-shaped anywhere in the file.
    assert not LEAK.findall(blob), LEAK.findall(blob)

    # And it went where the value was, rather than the parameter disappearing:
    # a reader has to be able to tell a redacted value from one the device
    # never published, because "absent" is evidence about the hardware.
    params = download["bus"]["devices"]["0x0603"]["params"]
    assert params["BluetoothDevice.BleAddress"] == DIAG.REDACTED


def test_nothing_a_reader_needs_goes_with_it() -> None:
    """The pass is a value hunt, so it must not cost the evidence.

    A dump is only worth having for what it says about the vehicle. Serial
    numbers pin hardware rather than a location and are how two devices of one
    class are told apart after a re-pairing, the owner's own label is how
    anybody knows which bottle is which, and the address *type* is what said
    these were public addresses rather than rotating ones -- which is the
    finding in #35, not a detail of it.
    """
    devices = _download(_bus())["bus"]["devices"]
    right = devices["0x0603"]["params"]

    assert right["Identify.SerialNr"] == "LC-Rechts"
    assert right["GasBtl.Name"] == "Rechts"
    assert right["GasBtl.FillLevelP"] == 100
    assert right["BluetoothDevice.BleAddressType"] == 0
    assert devices["0x0201"]["params"]["Identify.SerialNr"] == "CBG04EU-E-35008040"
    # The panel's product name is not its advertised name and stays too.
    assert devices["0x0101"]["params"]["Identify.Name"] == "iNet X Panel"


def test_every_spelling_is_caught_wherever_it_sits() -> None:
    """Depth and spelling, on the paths a key name cannot reach.

    The dashed form and the advertised name are both in downloads already --
    ``tools/import_dump.py`` hunts all three on the way into ``dumps/``. What
    is checked here is that the writer's own pass reaches as far: inside a
    list, inside a dict nested in a parameter's value, and in a config-entry
    key nobody has added to ``TO_REDACT`` because nobody knew it would carry
    one.
    """
    bus = _bus()
    # A structured parameter value, the shape ErrCode arrives in, carrying an
    # address in a nested dict inside a list.
    bus.update(
        "BleDeviceManagement",
        "Slots",
        [{"slot": 1, "peer": "AA-BB-CC-11-22-33"}, {"slot": 2, "peer": None}],
        PANEL,
    )
    bus.unattributed["System.Peer"] = "iNetX-FFB4D1"

    download = _download(
        bus,
        entry_dict={
            "title": "Truma",
            # Not in TO_REDACT, and that is the point: the entry grows keys.
            "some_future_key": {"seen": ["AA:BB:CC:11:22:33", "iNetX-FFB4D1"]},
        },
    )
    blob = json.dumps(download)

    assert not LEAK.findall(blob), LEAK.findall(blob)
    slots = download["bus"]["devices"]["0x0101"]["params"]["BleDeviceManagement.Slots"]
    assert slots[0]["peer"] == DIAG.REDACTED
    # The reading beside it is untouched, so the shape still says what arrived.
    assert slots[0]["slot"] == 1 and slots[1]["peer"] is None
    assert download["bus"]["unattributed"]["System.Peer"] == DIAG.REDACTED


def test_the_redacted_download_still_reads_back() -> None:
    """``tools/dump_bus.py`` has to open what the writer wrote.

    Redaction happens on the way out, so it is the writer's own output that
    every reader sees. A pass that produced something ``from_diagnostics``
    could not parse would break the one tool people are asked to run on a
    download.
    """
    bus = BUS.from_diagnostics(_download(_bus()))

    assert set(bus.devices) == {PANEL, HEATER, BOTTLE_RIGHT, BOTTLE_LEFT}
    assert bus.device(BOTTLE_RIGHT).label == "Rechts"
    right = bus.device(BOTTLE_RIGHT)
    assert right.get("BluetoothDevice", "BleAddress") == DIAG.REDACTED
    assert BUS.dump(bus).strip()


def _main() -> None:
    stubs.run_tests(globals(), "diagnostics redaction")


if __name__ == "__main__":
    _main()


def test_the_bond_is_reported_split_the_way_it_exists() -> None:
    """Charter C6: no failure may need a shell on the box to diagnose.

    A bond has two independent holders -- this host (BlueZ's device record) and
    the panel (its own list of about four devices) -- and when they disagree the
    symptom is "it worked yesterday and now nothing connects". The host's half is
    readable; the panel's half is not readable at all, only observable through
    what it does. Reporting one and silently omitting the other invites exactly
    the wrong conclusion, so the second is stated as unreadable with the evidence
    that stands in for it.
    """
    download = _download(_bus())
    assert "host_bond" in download, sorted(download)
    assert "panel_bond" in download, sorted(download)
    assert download["panel_bond"]["readable"] is False
    assert download["panel_bond"]["evidence"]["session_transport"] in ("local", "proxy")
    # No address on this entry, so there is nothing to look up -- and the reason
    # must say that rather than reporting "no bond", which is a different fact.
    assert download["host_bond"]["present"] is False
    assert "address" in download["host_bond"]["reason"]
    assert not LEAK.findall(json.dumps(download["host_bond"]))


def test_a_download_survives_an_unreachable_bluez() -> None:
    """The common case off the van: no system bus, no BlueZ, no crash.

    A diagnostics download is what people attach to an issue, and it is often
    produced on a machine that has nothing to do with the vehicle. Failing the
    download because D-Bus is absent would lose the rest of the evidence with it.
    """
    download = _download(_bus(), address="50:98:93:FF:B4:D1")
    assert download["host_bond"]["present"] is False
    assert "reason" in download["host_bond"]
    # Whatever the reason is, it is not an exception and not an address leak.
    assert not LEAK.findall(json.dumps(download["host_bond"]))
