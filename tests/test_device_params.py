#!/usr/bin/env python3
"""Offline checks that two devices reporting one topic stay apart.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``coordinator._on_frame`` runs, and the frames it is fed are built and parsed
with the real ``truma.protocol``.

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
4. neither the message broker nor our own assigned address is filed as a
   device, and a value from one is not attributed to anybody,
5. two descriptions of one parameter are not blended into a third that no
   device gave, and each device's own is what its control is built from,
6. the topics with more than one device behind them are named rather than
   left to be worked out by hand,
7. a device's identity, class and instance come off the bus rather than out
   of a table -- and the one address named by a table is named only where the
   bus names nothing,
8. and a diagnostics download names the devices in the form addresses are
   read and quoted in, not as decimal.

Run: ``python3 tests/test_device_params.py`` (needs ``cbor2``).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

APP_ADDR = 0x0501

# Addresses as measured on the reporter's vehicle. A device address is
# class << 8 | instance, which is exactly why two of a class collide.
PANEL = 0x0101
COMBI = 0x0201
ROOF_AC = 0x0406
BOTTLE_LEFT = 0x0603
BOTTLE_RIGHT = 0x0604
# The panel's own Bluetooth side -- same class as the bottles, which is the
# point: it is told apart by what it publishes, not by its class.
BLE_MGMT = 0x0601
BROKER = 0x0000

stubs.install_homeassistant()
stubs.stub_transport()
TC = stubs.load_truma("const")
PROTO = stubs.load_truma("protocol")
BUS = stubs.load("bus")
stubs.load("const")
COORD = stubs.load("coordinator")
DIAG = stubs.load("diagnostics")

import cbor2  # noqa: E402  - after the stubs, which put the package on sys.path


class _Coord:
    """Carries only what ``_on_frame`` and ``device_info`` touch."""

    hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=lambda: 0.0))
    unique_id = "Truma iNetX-FFB4D1"
    last_update_success = True
    # Which kind of address this host connects on; the download reports it, so
    # a coordinator that never ran a session has to answer "not known yet".
    address_kind = None
    _client = None
    poll_interval = 0
    # What setup fills in once it has registered the panel with the device
    # registry, and what every other device then hangs off.
    hub_device_id = "panel-device-id"

    def __init__(self) -> None:
        self._bus = BUS.Bus()
        self._bus.assigned_addr = APP_ADDR
        self._last_frame = 0.0
        self.updates = 0
        self._writes_pending = 0

    @property
    def data(self):
        return self._bus

    def async_set_updated_data(self, _data) -> None:
        self.updates += 1

    _on_frame = COORD.TrumaCoordinator._on_frame
    device_info = COORD.TrumaCoordinator.device_info
    async_write = COORD.TrumaCoordinator.async_write
    _client_for_write = COORD.TrumaCoordinator._client_for_write


class _Client:
    """A transport that records the frames handed to it."""

    connected = True
    assigned_addr = APP_ADDR

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, frame: bytes, *, probe: bool = False) -> bool:
        parsed = PROTO.parse_v3_frame(frame)
        assert parsed is not None
        self.sent.append(parsed)
        return True


def _report(coord: _Coord, src: int, topic: str, param: str, value) -> None:
    """One device publishes one value, through the real frame path."""
    _deliver(coord, src, {"tn": topic, "pn": param, "v": value})


def _describe(coord: _Coord, src: int, topic: str, param: str, **entry) -> None:
    """One device publishes a value *and* its own description of it."""
    _deliver(coord, src, {"tn": topic, "pn": param, **entry})


def _deliver(coord: _Coord, src: int, payload: dict) -> None:
    frame = PROTO.build_v3_frame(
        APP_ADDR, src, TC.CTRL_MBP, TC.MBP_INFO, 0, cbor2.dumps(payload)
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

    bus = coord._bus
    assert bus.device(COMBI).get("AirCirculation", "FanLevel") == 4
    assert bus.device(ROOF_AC).get("AirCirculation", "FanLevel") == 2
    # ...and there is no third answer for anything to read instead.
    assert not hasattr(bus, "raw_params")


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

    bus = coord._bus
    for addr, name, level in (
        (BOTTLE_RIGHT, "Rechts", 100),
        (BOTTLE_LEFT, "Links", 49),
    ):
        device = bus.device(addr)
        assert device.get("GasBtl", "Name") == name
        assert device.get("GasBtl", "FillLevelP") == level, (
            "the level under this name came from the other bottle"
        )


def test_the_panel_keeps_its_own_identity() -> None:
    """Every device describes itself under Identify, so a flat one is a race.

    On the reporter's vehicle Identify.Name read "Truma LevelControl" and
    Identify.SerialNr a bottle sensor's, rather than the panel's.
    """
    coord = _Coord()
    _report(coord, PANEL, "Identify", "Name", "iNet X Pro Panel")
    _report(coord, PANEL, "Identify", "SerialNr", "23456789")
    _report(coord, BOTTLE_LEFT, "Identify", "Name", "Truma LevelControl")
    _report(coord, BOTTLE_LEFT, "Identify", "SerialNr", "99887766")

    bus = coord._bus
    assert bus.device(PANEL).name == "iNet X Pro Panel"
    assert bus.device(PANEL).serial == "23456789"
    assert bus.device(BOTTLE_LEFT).name == "Truma LevelControl"
    assert bus.device(BOTTLE_LEFT).serial == "99887766"


def test_a_device_knows_its_class_and_instance() -> None:
    """The address is structured, and that structure is what names devices."""
    coord = _Coord()
    _report(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", 49)
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)

    left, right = coord._bus.device(BOTTLE_LEFT), coord._bus.device(BOTTLE_RIGHT)
    assert (left.cls, left.instance) == (0x06, 3)
    assert (right.cls, right.instance) == (0x06, 4)


def test_two_of_a_kind_are_two_named_devices() -> None:
    """Both bottles answer to "Truma LevelControl"; the instance separates them.

    Without it the user is given two identically-named devices, which is no
    better than the flat reading that mixed the two bottles up in the first
    place.
    """
    coord = _Coord()
    for addr in (BOTTLE_LEFT, BOTTLE_RIGHT):
        _report(coord, addr, "Identify", "Name", "Truma LevelControl")
        _report(coord, addr, "GasBtl", "FillLevelP", 50)

    assert coord.device_info(BOTTLE_LEFT)["name"] == "Truma LevelControl 3"
    assert coord.device_info(BOTTLE_RIGHT)["name"] == "Truma LevelControl 4"
    # Each hangs off the panel, which is the gateway they are all behind.
    assert coord.device_info(BOTTLE_LEFT)["via_device_id"] == "panel-device-id"
    # ...and the panel is that hub rather than a device below it.
    assert "via_device_id" not in coord.device_info(PANEL)
    assert "via_device" not in coord.device_info(PANEL)


def test_a_device_that_names_itself_nothing_is_named_by_its_address() -> None:
    """A class is not a product: 0x04 covers roof units and electrical blocks.

    So a device that publishes no Identify.Name is named after the address it
    is at, which is checkable, rather than after a guess at what kind of thing
    lives at that class.
    """
    coord = _Coord()
    _report(coord, ROOF_AC, "AirCirculation", "FanLevel", 2)

    assert coord.device_info(ROOF_AC)["name"] == "Bus device 0x0406"


def test_the_panels_bluetooth_side_is_named_though_it_names_nothing() -> None:
    """The one address whose function is known without it saying so.

    Measured on the van: 0x0601 publishes seven BleDeviceManagement
    parameters and no Identify.Name at all, so the rule above would leave the
    panel's own Bluetooth side reading "Bus device 0x0601". It is not a guess
    at a class the way a roof unit would be -- DEVICE_SEED already names this
    address for the same reason.
    """
    coord = _Coord()
    _report(coord, BLE_MGMT, "BleDeviceManagement", "NrFreeSlots", 2)

    assert coord.device_info(BLE_MGMT)["name"] == "Bluetooth management"
    assert coord.device_info(BLE_MGMT)["via_device_id"] == "panel-device-id"


def test_the_hub_is_named_the_way_this_home_assistant_names_hubs() -> None:
    """``via_device`` is deprecated as of 2026.8 and stops working in 2027.8.

    ``via_device_id`` replaced it, and names the panel by the device
    registry's own id rather than by its identifiers -- which is why setup
    registers the panel before any platform is forwarded, and hands the id
    back here.

    Both are kept, because this runs on whatever Home Assistant the vehicle
    has: an older one does not merely ignore the new key, it rejects the
    device info whole, and the vehicle ends up with no devices at all rather
    than with a flat list.
    """
    coord = _Coord()
    _report(coord, BLE_MGMT, "BleDeviceManagement", "NrFreeSlots", 2)

    # Setup has not run yet, or this Home Assistant has never heard of the
    # new key: the old one still says where the device belongs.
    coord.hub_device_id = None
    info = coord.device_info(BLE_MGMT)
    assert info["via_device"] == ("truma_inetx", coord.unique_id)
    assert "via_device_id" not in info

    coord.hub_device_id = "panel-device-id"
    info = coord.device_info(BLE_MGMT)
    assert info["via_device_id"] == "panel-device-id"
    # Never both: Home Assistant would have two answers to one question.
    assert "via_device" not in info


def test_a_device_that_names_itself_there_keeps_its_own_name() -> None:
    """Which is what makes naming an address safe at all.

    0x06 is the class the gas-bottle sensors are on -- 0x0603 and 0x0604 on
    the vehicle in #9 -- and they do name themselves. The address table is
    consulted only after the bus's own name, so it can never overwrite one.
    """
    coord = _Coord()
    _report(coord, BLE_MGMT, "Identify", "Name", "Truma LevelControl")
    _report(coord, BLE_MGMT, "GasBtl", "FillLevelP", 49)

    assert coord.device_info(BLE_MGMT)["name"] == "Truma LevelControl"


def test_neither_pseudo_address_is_a_device() -> None:
    """Address 0 is the message broker, and one address is our own."""
    coord = _Coord()
    _report(coord, BROKER, "System", "FlameStatus", 1)
    _report(coord, APP_ADDR, "System", "FlameStatus", 1)

    assert BROKER not in coord._bus.devices
    assert APP_ADDR not in coord._bus.devices
    # The values are not silently gone either -- they are parked where a
    # download still shows them and no entity reads them.
    assert coord._bus.unattributed == {"System.FlameStatus": 1}


def test_two_descriptions_are_not_blended_into_a_third() -> None:
    """A description is as much its author's property as a value is.

    A Combi's fan and a roof air conditioner's need not run to the same
    maximum. Merged across the bus, the range comes from one device and the
    enum from the other, and the result is a description that no device ever
    gave.
    """
    coord = _Coord()
    _describe(coord, COMBI, "AirCirculation", "FanLevel", v=4, min=0, max=4,
              enum=[{"n": "Off", "a": True, "v": 0},
                    {"n": "Max", "a": True, "v": 4}])
    _describe(coord, ROOF_AC, "AirCirculation", "FanLevel", v=2, min=1, max=10)

    combi = coord._bus.device(COMBI).meta("AirCirculation", "FanLevel")
    roof = coord._bus.device(ROOF_AC).meta("AirCirculation", "FanLevel")
    assert (combi["min"], combi["max"]) == (0, 4), combi
    assert (roof["min"], roof["max"]) == (1, 10), roof
    assert "enum" not in roof, "the roof unit described no enum and was given one"
    # Each device answers for its own range, so a slider built on one of them
    # cannot offer a level the other's hardware would refuse.
    assert coord._bus.device(COMBI).bounds("AirCirculation", "FanLevel") == (0, 4)
    assert coord._bus.device(ROOF_AC).bounds("AirCirculation", "FanLevel") == (1, 10)


def test_allowed_values_are_asked_of_one_device() -> None:
    """A control offered a merged enum offers values its device refuses."""
    coord = _Coord()
    _describe(coord, COMBI, "AirCirculation", "FanLevel", v=1,
              enum=[{"n": "Off", "a": True, "v": 0},
                    {"n": "Low", "a": True, "v": 1}])
    _describe(coord, ROOF_AC, "AirCirculation", "FanLevel", v=7,
              enum=[{"n": "Low", "a": True, "v": 1},
                    {"n": "High", "a": True, "v": 7}])

    bus = coord._bus
    assert bus.device(COMBI).allowed_values("AirCirculation", "FanLevel") == [0, 1]
    assert bus.device(ROOF_AC).allowed_values("AirCirculation", "FanLevel") == [1, 7]
    # And a write is judged by the device it is going to, not by the bus.
    assert bus.validate_write(COMBI, "AirCirculation", "FanLevel", 7)[0] is False
    assert bus.validate_write(ROOF_AC, "AirCirculation", "FanLevel", 7)[0] is True


def test_contested_topics_names_what_cannot_be_trusted() -> None:
    """The condition a flat view is wrong under, stated rather than implied."""
    coord = _Coord()
    _report(coord, COMBI, "AirCirculation", "FanLevel", 4)
    _report(coord, ROOF_AC, "AirCirculation", "FanLevel", 2)
    _report(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", 49)
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)
    _report(coord, PANEL, "RoomClimate", "Mode", 3)

    bus = coord._bus
    assert bus.publishers("AirCirculation") == [COMBI, ROOF_AC]
    assert bus.contested_topics() == {
        "AirCirculation": [COMBI, ROOF_AC],
        "GasBtl": [BOTTLE_LEFT, BOTTLE_RIGHT],
    }, "a topic with one owner is not contested and must not be listed"
    # A topic exactly one device publishes still has a single answer, which is
    # what the climate entity reads RoomClimate through.
    assert bus.relayed("RoomClimate", "Mode") == 3
    assert bus.relayed("AirCirculation", "FanLevel") is None, (
        "two publishers means there is no single answer, and inventing one is "
        "the bug this rewrite removes"
    )


def test_a_write_is_addressed_to_the_device_the_entity_belongs_to() -> None:
    """The real write path, not a stub of it -- #10 lived in exactly this step.

    A fake coordinator records (addr, topic, param, value) and proves nothing
    about the frame that goes out, so this drives ``async_write`` itself and
    reads the destination out of the frame the transport was handed.
    """
    coord = _Coord()
    client = _Client()
    coord._client = client
    _report(coord, ROOF_AC, "AirCooling", "TgtTemp", 220)

    asyncio.run(coord.async_write(ROOF_AC, "AirCooling", "TgtTemp", 170))
    assert [frame["dest"] for frame in client.sent] == [ROOF_AC], (
        "cooling is addressed to the heater again, which swallows it"
    )

    # ...and the one topic the panel relays keeps going to the panel, however
    # the value reaches us and whichever device the entity sits on.
    asyncio.run(coord.async_write(COMBI, "RoomClimate", "Mode", 3))
    assert client.sent[-1]["dest"] == PANEL


def test_a_write_a_device_says_it_will_refuse_is_not_sent() -> None:
    """Refused with the device's own claim quoted, rather than swallowed."""
    coord = _Coord()
    client = _Client()
    coord._client = client
    _describe(coord, COMBI, "AirCirculation", "FanLevel", v=4, min=0, max=4)

    try:
        asyncio.run(coord.async_write(COMBI, "AirCirculation", "FanLevel", 9))
    except RuntimeError as exc:  # HomeAssistantError is stubbed as RuntimeError
        assert "0-4" in str(exc), exc
    else:
        raise AssertionError("a value the device rejects was sent anyway")
    assert client.sent == [], "the frame went out before being validated"


def test_a_download_names_the_devices_the_way_they_are_quoted() -> None:
    """A dump is the evidence someone pastes into an issue; 0x0603, not 1539."""
    coord = _Coord()
    _describe(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", v=49, max=100)
    _report(coord, BOTTLE_LEFT, "Identify", "Name", "Truma LevelControl")
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)

    entry = types.SimpleNamespace(
        runtime_data=coord, as_dict=lambda: {"title": "Truma"}
    )
    dumped = json.loads(json.dumps(
        asyncio.run(DIAG.async_get_config_entry_diagnostics(None, entry))
    ))
    bus = dumped["bus"]

    devices = bus["devices"]
    assert set(devices) == {"0x0603", "0x0604"}, devices
    assert devices["0x0603"]["params"]["GasBtl.FillLevelP"] == 49
    assert devices["0x0604"]["params"]["GasBtl.FillLevelP"] == 100
    # The address is rendered in the same form inside the record as out.
    assert devices["0x0603"]["addr"] == "0x0603"
    assert devices["0x0603"]["instance"] == 3
    assert devices["0x0603"]["name"] == "Truma LevelControl"
    # Descriptions are filed and rendered per device too.
    assert devices["0x0603"]["param_meta"]["GasBtl.FillLevelP"]["max"] == 100
    # And the download says outright which topics have more than one device.
    assert bus["contested_topics"] == {"GasBtl": ["0x0603", "0x0604"]}


def test_the_terminal_dump_shows_the_bus_per_device() -> None:
    """The same evidence without Home Assistant in the way."""
    coord = _Coord()
    _report(coord, BOTTLE_LEFT, "Identify", "Name", "Truma LevelControl")
    _describe(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", v=49, min=0, max=100)
    _report(coord, BOTTLE_RIGHT, "GasBtl", "FillLevelP", 100)

    text = BUS.dump(coord._bus)
    assert "0x0603" in text and "0x0604" in text
    assert "Truma LevelControl" in text
    assert "instance 3" in text and "instance 4" in text
    assert "0..100" in text
    assert "GasBtl" in text.split("more than one publisher")[1]


def test_a_download_reads_back_into_the_same_bus() -> None:
    """The dump tool's whole point: read somebody's download as a bus.

    A diagnostics download is what an issue report actually contains, and
    until it can be loaded back into the object the integration runs on, the
    only way to look at somebody's bus is to squint at their JSON.
    """
    coord = _Coord()
    _report(coord, BOTTLE_LEFT, "Identify", "Name", "Truma LevelControl")
    _describe(coord, BOTTLE_LEFT, "GasBtl", "FillLevelP", v=49, min=0, max=100)
    _report(coord, BROKER, "System", "FlameStatus", 1)

    entry = types.SimpleNamespace(
        runtime_data=coord, as_dict=lambda: {"title": "Truma"}
    )
    dumped = json.loads(json.dumps(
        asyncio.run(DIAG.async_get_config_entry_diagnostics(None, entry))
    ))
    restored = BUS.from_diagnostics(dumped)

    device = restored.device(BOTTLE_LEFT)
    assert device.name == "Truma LevelControl"
    assert device.get("GasBtl", "FillLevelP") == 49
    assert device.bounds("GasBtl", "FillLevelP") == (0, 100)
    assert restored.unattributed == {"System.FlameStatus": 1}
    assert restored.assigned_addr == APP_ADDR


def _main() -> None:
    stubs.run_tests(globals(), "per-device parameters")


if __name__ == "__main__":
    _main()
