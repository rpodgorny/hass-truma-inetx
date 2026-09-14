"""The panel's bus, modelled the way the panel models it.

The Truma iNet X Panel is not a heater remote. It is a multi-homed gateway: a
TIN bus of Truma appliances, a CI bus of vehicle electrics and third-party air
conditioners, a CAN bus, and Bluetooth for gas sensors and for us. What it
publishes is therefore a *bus*, and every frame on it says which device spoke:

    (src address, topic, parameter) -> value, plus the panel's own description

A device address is ``class << 8 | instance``, and the instance is assigned at
pairing and renumbered when a device is re-paired. So no static map of the bus
can be right for every installation, and none is attempted here.

This module holds that model and nothing else. It imports no Home Assistant --
everything in it is testable, and dumpable, with no HA install: ``_main()``
below reads a diagnostics download or talks to a panel over Bluetooth, and
``tools/dump_bus.py --help`` is how it is run.

What this replaced, and why
---------------------------

Until this rewrite every value was flattened into one dict keyed
``Topic.Param``, and ~30 typed fields were assigned from a fixed
``(topic, param) -> field`` table. Both assert that a topic has exactly one
owner, which is simply not true of a bus: subscription names *topics* and is
addressed to the broker, so every device implementing a topic publishes under
it.

Issue #9 is what that looks like on a vehicle. Not a merely stale reading, but
a plausible wrong one assembled out of two devices::

    GasBtl.Name        "Rechts"     <- the right-hand bottle
    GasBtl.FillLevelP  49           <- the *left* one; the right is at 100

...and the same for a Combi's ``AirCirculation.FanLevel`` against a roof air
conditioner's, and for ``Identify``, which every device on the bus publishes,
so the panel's own name and serial ended up being whichever gas sensor spoke
last.

There is no flat view here to fall back to. It was right wherever exactly one
device reported a topic -- which is most of them, which is why it survived so
long -- and wrong in exactly the cases anybody would want a second device for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from .truma.const import DEV_APP_DEFAULT, DEV_PANEL


class ActiveState(IntEnum):
    """The protocol's three-state "is this running" family (type 105).

    ``AirHeating.Active``, ``WaterHeating.Active``, ``AirCirculation.Active``
    and ``System.FlameStatus`` all take these. IDLE is the appliance standing
    by: measured on a Combi 6 E against an independent shore-power meter
    (#15), the value went ACTIVE -> IDLE in the same second the draw fell from
    1787 W to 105 W.
    """

    OFF = 0
    ACTIVE = 1
    IDLE = 2


# Command routing: topic -> a fixed destination that is not the topic's owner.
#
# Nearly everything is addressed to the device that publishes it, which is the
# only durable answer: addresses are renumbered when a device is re-paired, so
# a table of them is wrong the moment somebody re-pairs a gas sensor.
#
# AirCooling used to be in this table, addressed to the heater -- which on the
# vehicles that actually have cooling is not the device that does it. Measured
# on a Combi 6 E with a Dometic FreshJet 2200 at 0x0406 (#10): a write of
# AirCooling.TgtTemp to 0x0201 is acknowledged by the transport and then
# silently dropped -- no error, nothing cools -- while the same write to
# 0x0406 is acknowledged and the roof unit starts.
#
# RoomClimate is the one genuine exception, and it is the panel's own topic:
# the panel relays the room mode to whichever appliance serves the room, and
# RoomClimate.TgtTemp only ever echoes back what we wrote. It has to keep
# going to the panel however the value reaches us.
COMMAND_DEST = {
    "RoomClimate": DEV_PANEL,
}

# Command validation of last resort: topic.param -> (min, max) or valid values.
#
# This is a guess assembled from the vehicles reported so far, and it rejects
# what it has not seen -- a Combi 6 E could not be put into automatic or
# cooling through this integration because [0, 3, 5] said those do not exist
# (#11). The panel describes every parameter for the vehicle it is installed
# in, so wherever it has spoken, it wins; this is only what is left when it
# has not.
PARAM_VALIDATION = {
    "RoomClimate.Mode": [0, 3, 5],
    "RoomClimate.TgtTemp": (160, 300),  # wire values
    "AirHeating.TgtTemp": (50, 300),
    "AirHeating.Mode": [0, 1],
    "AirCirculation.FanLevel": (0, 10),
    "AirCirculation.Active": [0, 1],
    "WaterHeating.Mode": [0, 1, 2],
    "WaterHeating.Active": [0, 1],
    "WaterHeating.BoostMode": [0, 1],
    "WaterHeating.FasterHeatingMode": [0, 1],
    "EnergySrc.DieselLevel": [0, 1],
    "EnergySrc.ElectricLevel": [0, 1, 2],
    "Switches.FreshWaterPump": [0, 1],
}

# The keys a parameter's own description arrives under, alongside "tn"/"pn"/
# "v". Panels send them on plain value updates as well as in the answer to a
# discovery request, so both paths feed Bus.learn_param().
_PARAM_META_KEYS = ("type", "perm", "avail", "min", "max")

# Every device on the bus describes itself under Identify. The panel names it
# under Identify.Name; the serial arrives under a parameter whose exact
# spelling is not pinned down -- "SerialNr" on the vehicle in #9, "SerialNumber"
# in Truma's own naming. Matching the prefix covers both, where guessing one
# spelling gives a device page with a blank serial and no hint which it was.
IDENTIFY_TOPIC = "Identify"
IDENTIFY_NAME = "Name"
_SERIAL_PREFIX = "Serial"


@dataclass
class Device:
    """One address on the bus, and everything it has said about itself."""

    addr: int
    # "Topic.Param" -> the value this device last published for it.
    params: dict[str, Any] = field(default_factory=dict)
    # "Topic.Param" -> what this device says the parameter *is*, as opposed to
    # what it currently reads. Per device, because a description is as much
    # the property of its author as a value is: a Combi's fan and a roof air
    # conditioner's need not run to the same maximum, and merging the two
    # produces a description no device on the bus ever gave.
    param_meta: dict[str, dict] = field(default_factory=dict)
    # Wall-clock of the last frame from this address. Not used for
    # availability -- the BLE link answers that for the whole bus -- but it is
    # what says whether a quiet device is quiet or gone.
    last_seen: float = 0.0

    @property
    def cls(self) -> int:
        """The device class half of the address (0x02 for a heater)."""
        return self.addr >> 8

    @property
    def instance(self) -> int:
        """The instance half, assigned at pairing and renumbered on re-pair."""
        return self.addr & 0xFF

    def get(self, topic: str, param: str) -> Any:
        """What this device last reported for a parameter, or None."""
        return self.params.get(f"{topic}.{param}")

    def reports(self, topic: str, param: str) -> bool:
        """Whether this device has ever published the parameter.

        This is the evidence the hardware behind it exists -- see
        ``entity.async_add_rows``. A device that has answered once but is
        quiet now is still hardware.
        """
        return f"{topic}.{param}" in self.params

    def meta(self, topic: str, param: str) -> dict:
        """This device's own description of a parameter; empty if none."""
        return self.param_meta.get(f"{topic}.{param}", {})

    def topics(self) -> set[str]:
        """Every topic this device has published under."""
        return {key.split(".", 1)[0] for key in self.params}

    @property
    def name(self) -> str | None:
        """The panel's name for this device, e.g. "Truma LevelControl"."""
        value = self.get(IDENTIFY_TOPIC, IDENTIFY_NAME)
        return str(value) if value not in (None, "") else None

    @property
    def serial(self) -> str | None:
        """This device's serial number, if it publishes one."""
        for key, value in self.params.items():
            topic, _, param = key.partition(".")
            if (
                topic == IDENTIFY_TOPIC
                and param.startswith(_SERIAL_PREFIX)
                and value not in (None, "")
            ):
                return str(value)
        return None

    def allowed_values(self, topic: str, param: str) -> list[int] | None:
        """The values *this device* says it can be set to, or None.

        A panel enumerates a parameter per installation, not per protocol. The
        van this was measured on has no air conditioner, and its
        ``RoomClimate.Mode`` enum is simply ``{0: Off, 3: Heating,
        5: Ventilating}`` -- 1 and 2 are absent rather than present-and-
        unavailable. A Combi 6 E reports automatic and cooling there (#11).
        Which is why no list written into this file can be right for both.

        ``None`` means this device described no enum, and the caller's own
        table is all there is.

        Only the values are returned, never the panel's names for them. Those
        arrive in the panel's display language -- the same three water-heating
        steps come back as ``40 / 60 / 70`` here and as Eco / Comfort / Hot on
        the Combi 6 E in #12 -- so they are evidence about which value means
        what, and they have no business reaching a user-facing string.
        """
        meta = self.meta(topic, param)
        names = meta.get("enum")
        if not names:
            return None
        unavailable = set(meta.get("enum_unavailable", ()))
        values = [
            int(value)
            for value in names
            if value not in unavailable and str(value).lstrip("-").isdigit()
        ]
        return sorted(values) or None

    def bounds(self, topic: str, param: str) -> tuple[int, int] | None:
        """This device's own range for a parameter, or None if it gave one end.

        ``number.py`` hard-coded 0-10 for the circulation fan, which is the
        Combi's range and was handed to every device including a roof air
        conditioner. The device that owns the parameter is the authority on
        how far it goes.
        """
        meta = self.meta(topic, param)
        low, high = meta.get("min"), meta.get("max")
        if not isinstance(low, int) or not isinstance(high, int) or low >= high:
            return None
        return low, high

    def writable(self, topic: str, param: str) -> bool | None:
        """Whether this device says the parameter may be written.

        ``perm`` is documented by the reverse-engineered protocol reference as
        "permission, Integer" and nothing more: 1 is the only value seen in a
        capture. So this answers ``None`` -- "the device did not say" --
        wherever there is no ``perm`` at all, and reads 0 as a refusal on the
        strength of the field's name.

        That inference is deliberately used only to refuse a write with a
        message naming the claim, never to withhold a control. Withholding one
        on a guess is the failure this repo has already paid for twice (the
        electric select and the diesel switch, both offered against hardware
        that had neither), and a wrong guess here would be invisible; a
        refusal that quotes the panel gets reported the same day.
        """
        perm = self.meta(topic, param).get("perm")
        if not isinstance(perm, int):
            return None
        return perm != 0


@dataclass
class Bus:
    """Every device the panel has let us hear from, and the link's own state."""

    devices: dict[int, Device] = field(default_factory=dict)

    # Values that arrived with no usable source address. Nothing reads these:
    # a value nobody claims cannot be attributed to a device, and filing it
    # under a plausible one is how #9 happened in the first place. They are
    # kept so a diagnostics download still shows them, because a bus that
    # published everything this way would otherwise look simply empty.
    unattributed: dict[str, Any] = field(default_factory=dict)

    last_update: float = 0.0
    connected: bool = False
    # The address the panel assigned *us* at registration. The panel puts it
    # in the src of some frames, so it has to be recognisable as not-a-device.
    assigned_addr: int = DEV_APP_DEFAULT

    # -- devices ---------------------------------------------------------

    def device(self, addr: int) -> Device:
        """The device at an address, created empty if it is new."""
        found = self.devices.get(addr)
        if found is None:
            found = self.devices[addr] = Device(addr)
        return found

    def note_seen(self, addr: int) -> None:
        """Record that an address sent us a frame.

        Parameter discovery is addressed device by device (see DEVICE_SEED),
        and a device that has spoken once is proof its address exists -- which
        matters because addresses are renumbered on re-pairing, so no fixed
        list can be right for every installation.

        The coordinator holds one bus across reconnects, so this accumulates:
        a device that turns up late in one session -- battery-powered gas
        sensors were measured taking minutes -- is asked directly from the
        next connect onwards.
        """
        self.device(addr).last_seen = time.time()

    @property
    def addresses(self) -> set[int]:
        """Every address that has spoken to us."""
        return set(self.devices)

    def _attributable(self, src: int | None) -> Device | None:
        """The device a frame is from, or None if it names none.

        0 is the message broker, not a device: it must not be learned as a
        write destination and it owns no parameters. Our own assigned address
        is not one either -- measured on the van, the panel puts it in src on
        some frames, and filing values under it would invent a device that is
        us.
        """
        if not isinstance(src, int) or src == 0 or src == self.assigned_addr:
            return None
        return self.device(src)

    # -- incoming values -------------------------------------------------

    def update(
        self, topic: str, param: str, value: Any, src: int | None = None
    ) -> None:
        """File a decoded value under the device that published it.

        ``src`` is the V3 header's source address. It is not optional in
        practice -- every frame carries one -- and a value that arrives
        without a usable one is parked in :attr:`unattributed` rather than
        guessed at.
        """
        self.last_update = time.time()
        device = self._attributable(src)
        if device is None:
            self.unattributed[f"{topic}.{param}"] = value
            return
        device.last_seen = self.last_update
        device.params[f"{topic}.{param}"] = value

    def learn_param(
        self, topic: str, param: str, entry: Any, src: int | None = None
    ) -> bool:
        """Record what a device says about a parameter. True if that is new.

        Every value the panel sends is wrapped in a description of the
        parameter carrying it: ``type``, ``perm`` (writable?), ``avail``,
        ``min``, ``max``, and ``enum`` -- a list of ``{"n": name, "a":
        available, "v": value}`` in which the panel names each value itself.
        All of it used to be dropped, which is why the meaning of a value has
        had to be measured on somebody's vehicle instead of read off the bus.

        ``a`` is per-installation, not per-protocol: a heater without a diesel
        burner still gets an enum that names the diesel value, marked
        unavailable. That distinction is worth keeping -- it says which values
        a given vehicle can actually produce.
        """
        if not isinstance(entry, dict):
            return False

        meta: dict = {
            key: entry[key] for key in _PARAM_META_KEYS if entry.get(key) is not None
        }

        elements = entry.get("enum")
        if isinstance(elements, list):
            names: dict = {}
            unavailable: list = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                value, name = element.get("v"), element.get("n")
                if not isinstance(value, int) or name is None:
                    continue
                names[str(value)] = str(name)
                if not element.get("a", True):
                    unavailable.append(str(value))
            if names:
                meta["enum"] = names
            if unavailable:
                meta["enum_unavailable"] = unavailable

        if not meta:
            return False

        device = self._attributable(src)
        if device is None:
            return False

        # Merge rather than replace. A plain value update may describe less
        # than the answer to a discovery request did, and dropping the enum
        # again on the next frame would defeat the whole point. Merging is
        # only safe *within* one device, which is the whole reason these are
        # kept per device: merged across the bus, an enum from one and a range
        # from the other blend into a third description that nothing gave.
        key = f"{topic}.{param}"
        known = device.param_meta.setdefault(key, {})
        changed = any(known.get(name) != value for name, value in meta.items())
        known.update(meta)
        return changed

    # -- reading across the bus ------------------------------------------

    def publishers(self, topic: str, param: str | None = None) -> list[int]:
        """Every device that has reported under a topic, lowest address first.

        More than one is the condition under which any bus-wide reading stops
        meaning what it says, and the reason nothing here offers one.
        """
        prefix = f"{topic}." if param is None else f"{topic}.{param}"
        exact = param is not None
        return sorted(
            addr
            for addr, device in self.devices.items()
            if any(
                key == prefix if exact else key.startswith(prefix)
                for key in device.params
            )
        )

    def contested_topics(self) -> dict[str, list[int]]:
        """Topics more than one device reports, each with its publishers.

        Worked out once rather than by hand from a diagnostics download.
        Usually empty, which is itself worth saying out loud: it is what made
        a flat model look correct for so long.
        """
        topics = {
            key.split(".", 1)[0]
            for device in self.devices.values()
            for key in device.params
        }
        found = {topic: self.publishers(topic) for topic in sorted(topics)}
        return {topic: addrs for topic, addrs in found.items() if len(addrs) > 1}

    def sole_publisher(self, topic: str, param: str) -> Device | None:
        """The one device publishing a parameter, or None if it is not one.

        For the handful of topics that are genuinely the bus's rather than an
        appliance's -- ``RoomClimate`` is the panel relaying the room mode to
        whichever appliance serves the room -- an entity bound to the heater
        still has to read the panel's value. Returning None as soon as two
        devices publish it is the point: a second publisher means there is no
        single answer, and inventing one is exactly what this rewrite removes.
        """
        addrs = self.publishers(topic, param)
        if len(addrs) != 1:
            return None
        return self.devices[addrs[0]]

    def relayed(self, topic: str, param: str) -> Any:
        """The value of a parameter exactly one device on the bus publishes."""
        device = self.sole_publisher(topic, param)
        return None if device is None else device.get(topic, param)

    # -- writes ----------------------------------------------------------

    def command_dest(self, addr: int, topic: str) -> int:
        """Where a write to a topic should actually go.

        The owning device, except for the topics the panel relays on our
        behalf (see COMMAND_DEST).
        """
        return COMMAND_DEST.get(topic, addr)

    def validate_write(
        self, addr: int, topic: str, param: str, value: int
    ) -> tuple[bool, str]:
        """Validate a write against what the owning device said about itself.

        The device's own description outranks PARAM_VALIDATION wherever it
        exists, because the table is a guess about hardware in general and the
        description is a statement about this vehicle.
        """
        device = self.devices.get(addr)
        if device is not None:
            if device.writable(topic, param) is False:
                return False, (
                    f"{topic}.{param}: 0x{addr:04X} describes it as read-only"
                )
            allowed = device.allowed_values(topic, param)
            if allowed is not None:
                if value not in allowed:
                    return False, (
                        f"{topic}.{param}: 0x{addr:04X} offers only {allowed}"
                    )
                return True, "ok"
            bounds = device.bounds(topic, param)
            if bounds is not None:
                if not bounds[0] <= value <= bounds[1]:
                    return False, (
                        f"{topic}.{param}: 0x{addr:04X} accepts "
                        f"{bounds[0]}-{bounds[1]}"
                    )
                return True, "ok"
        return validate_against_table(topic, param, value)

    # -- units -----------------------------------------------------------

    @staticmethod
    def wire_to_celsius(wire_value: int | None) -> float | None:
        """Convert a wire value (tenths of a degree) to Celsius."""
        if wire_value is None:
            return None
        return wire_value / 10.0


def validate_against_table(topic: str, param: str, value: int) -> tuple[bool, str]:
    """Check a write against PARAM_VALIDATION, for params nobody described."""
    key = f"{topic}.{param}"
    rule = PARAM_VALIDATION.get(key)
    if rule is None:
        return True, "ok"  # unknown param, allow
    if isinstance(rule, list):
        if value not in rule:
            return False, f"{key}: value {value} not in {rule}"
    elif isinstance(rule, tuple):
        if value < rule[0] or value > rule[1]:
            return False, f"{key}: value {value} not in range {rule[0]}-{rule[1]}"
    return True, "ok"


def dump(bus: Bus) -> str:
    """The bus as a per-device table, for a terminal.

    This is the view the integration is built on, and the one an issue needs:
    which devices are on the bus, what each of them publishes, and what each
    of them says those parameters are. Flat dumps could not show it.
    """
    lines: list[str] = []
    for addr in sorted(bus.devices):
        device = bus.devices[addr]
        title = device.name or "unnamed"
        serial = f" serial {device.serial}" if device.serial else ""
        count = len(device.params)
        lines.append(
            f"0x{addr:04X}  class 0x{device.cls:02X} instance {device.instance}"
            f"  {title}{serial}  ({count} parameter{'' if count == 1 else 's'})"
        )
        for key in sorted(device.params):
            meta = device.param_meta.get(key) or {}
            notes = []
            if isinstance(meta.get("min"), int) and isinstance(meta.get("max"), int):
                notes.append(f"{meta['min']}..{meta['max']}")
            if meta.get("enum"):
                notes.append(
                    "enum " + ", ".join(
                        f"{value}={name}" for value, name in sorted(meta["enum"].items())
                    )
                )
            if meta.get("perm") is not None:
                notes.append(f"perm {meta['perm']}")
            if meta.get("avail") is not None:
                notes.append(f"avail {meta['avail']}")
            suffix = f"   [{'; '.join(notes)}]" if notes else ""
            lines.append(f"    {key:<44} {device.params[key]!r}{suffix}")
        lines.append("")
    contested = bus.contested_topics()
    if contested:
        lines.append("Topics with more than one publisher -- no flat reading of")
        lines.append("these can mean anything:")
        for topic, addrs in contested.items():
            lines.append(
                f"    {topic:<24} {', '.join(f'0x{a:04X}' for a in addrs)}"
            )
        lines.append("")
    if bus.unattributed:
        lines.append("Values that named no source device (nothing reads these):")
        for key in sorted(bus.unattributed):
            lines.append(f"    {key:<44} {bus.unattributed[key]!r}")
        lines.append("")
    return "\n".join(lines)


# -- standalone dump tool ------------------------------------------------
#
# Run it through ``tools/dump_bus.py``, which puts this file's package on
# sys.modules without executing the integration's Home Assistant entry point.
# Nothing below needs Home Assistant; the launcher exists only because the
# package __init__ above it does.


def from_diagnostics(payload: dict) -> Bus:
    """Rebuild a bus from a Home Assistant diagnostics download.

    A download is what an issue report actually contains, so being able to
    read one back into the same object the integration runs on is the
    difference between "paste the JSON and let me squint at it" and looking at
    somebody's bus the way they see it.
    """
    bus = Bus()
    section = payload.get("bus") or payload
    for key, record in (section.get("devices") or {}).items():
        addr = int(str(record.get("addr", key)), 16)
        device = bus.device(addr)
        device.params.update(record.get("params") or {})
        device.param_meta.update(record.get("param_meta") or {})
        device.last_seen = record.get("last_seen") or 0.0
    bus.unattributed.update(section.get("unattributed") or {})
    assigned = section.get("assigned_addr")
    if isinstance(assigned, str):
        bus.assigned_addr = int(assigned, 16)
    return bus


async def _live(name: str | None, identity: dict, settle: float) -> Bus:
    """Connect to a panel, run a session, and hand back what it published.

    Imported here rather than at module scope: reading a download needs
    neither bleak nor a Bluetooth adapter, and most of the time that is what
    this tool is used for.
    """
    from bleak import BleakScanner

    from . import session
    from .ble import TrumaBleClient
    from .const import LOCAL_NAME_PREFIX
    from .truma.const import SERVICE_UUID

    def _is_panel(device, advert) -> bool:
        # The service UUID is advertised even in add-device mode, when the
        # local name may be absent, so it is the more reliable of the two.
        if SERVICE_UUID.lower() in {uuid.lower() for uuid in advert.service_uuids}:
            return name is None or device.name == name
        return bool(device.name) and device.name == name

    print("scanning...", file=sys.stderr)
    device = await BleakScanner.find_device_by_filter(_is_panel, timeout=20)
    if device is None:
        raise SystemExit(
            f"no panel found (looked for {name or LOCAL_NAME_PREFIX + '*'}); "
            "is it in range, and is nothing else holding a link to it?"
        )

    bus = Bus()
    client = TrumaBleClient(identity)
    label = device.name or device.address
    client.on_data(lambda parsed: session.handle_frame(bus, parsed, client, label))
    print(f"connecting to {label}...", file=sys.stderr)
    await client.connect(device)
    try:
        await session.run_startup(
            client, bus, identity, label, asyncio.get_running_loop().time
        )
        # Battery-powered sensors were measured taking minutes to wake up, so
        # give the late ones a chance rather than reporting a partial bus as
        # if it were the whole one.
        if settle:
            print(f"listening for another {settle:.0f}s...", file=sys.stderr)
            await asyncio.sleep(settle)
    finally:
        await client.disconnect()
    return bus


def _main(argv: list[str] | None = None) -> int:
    """Dump a bus, from a diagnostics download or from real hardware."""
    parser = argparse.ArgumentParser(
        prog="tools/dump_bus.py",
        description=(
            "Print a Truma iNet X panel's bus, device by device: what each "
            "device publishes and what it says those parameters are."
        ),
    )
    parser.add_argument(
        "download",
        nargs="?",
        help="a Home Assistant diagnostics download to read instead of "
             "connecting to anything",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="connect to a panel over Bluetooth and dump what it publishes",
    )
    parser.add_argument(
        "--name",
        help="the panel's advertised name, e.g. 'Truma iNetX-FFB4D1'; "
             "without it the first panel that answers is used",
    )
    parser.add_argument(
        "--identity",
        help="the app identity the panel is bonded to, as Home Assistant "
             "stored it: config/.storage/truma_inetx_<entry id>. A panel only "
             "talks to an identity it has been paired with, so a live dump "
             "needs the one that did the pairing",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=30.0,
        help="seconds to keep listening after startup, for devices that are "
             "slow to wake (default: 30)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the bus as JSON, not a table"
    )
    parser.add_argument("--debug", action="store_true", help="log every frame")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    if args.live:
        identity = {"username": "truma dump"}
        if args.identity:
            stored = json.loads(Path(args.identity).read_text("utf-8"))
            # Home Assistant wraps what it stores in {"version":…, "data":…}.
            stored = stored.get("data", stored)
            identity = {
                key: stored[key]
                for key in ("muid", "uuid", "username")
                if key in stored
            }
        bus = asyncio.run(_live(args.name, identity, args.settle))
    elif args.download:
        bus = from_diagnostics(json.loads(Path(args.download).read_text("utf-8")))
    else:
        parser.error("give a diagnostics download to read, or --live")
        return 2

    if args.json:
        print(json.dumps(
            {
                f"0x{addr:04X}": {
                    "params": device.params,
                    "param_meta": device.param_meta,
                }
                for addr, device in sorted(bus.devices.items())
            },
            indent=2,
            ensure_ascii=False,
        ))
    else:
        print(dump(bus))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
