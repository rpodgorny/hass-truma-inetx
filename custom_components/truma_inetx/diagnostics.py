"""Diagnostics for Truma iNet X (BLE)."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import describe_scan_modes, scan_mode_report
from .coordinator import TrumaConfigEntry

# The BLE address is a resolvable private address that still pins the panel to
# a location, and muid/uuid are the persisted app identity the panel bonds
# against. The bus carries addresses too, but not under names like these --
# see ``_ADDRESS_SHAPED`` below, which is what covers them.
# discovery_keys is here because redaction works on key names and that
# subtree hides the address inside a value: Home Assistant serialises the key
# as ``"repr": "DiscoveryKey(domain='bluetooth', key='76:32:EF:78:FD:1A',
# version=1)"``, which no address-named key matches. Every download written
# before this line carried the panel's address in it, the three attached to
# issue #22 included. The value pass below would now catch it as well; the key
# stays because the whole subtree is of no use to a reader anyway.
TO_REDACT = {
    CONF_ADDRESS,
    CONF_NAME,
    "title",
    "unique_id",
    "address",
    "muid",
    "uuid",
    "discovery_keys",
}

# Home Assistant's own marker, so a download carries one spelling however the
# value was reached.
REDACTED = "**REDACTED**"

# An address in every spelling a download has been seen carrying, and the
# panel's advertised name, whose last three bytes are its identity address.
#
# Key names cannot find these, which is why ``TO_REDACT`` is not the whole
# answer. On the bus a key name is the panel's own vocabulary rather than ours,
# and the addresses that are on it sit in *values* of keys that say nothing:
# the LevelControl gas-bottle sensors publish their own MAC as an ordinary
# string parameter, described as ``{"type": 4, "avail": 1}`` like any other,
# and measured on the vehicle of #35 as
#
#     "0x0603": {"params": {"BluetoothDevice.BleAddress": "90:7B:C6:..",
#                           "BluetoothDevice.BleAddressType": 0, ...
#
# with ``BleAddressType: 0`` saying they are public, so permanent rather than
# rotating. One of the two shared a prefix with that panel's identity address.
# Redacting only ``BleAddress`` by name would cover those two and nothing a
# device publishes next under another name.
#
# Values only, never keys: two addresses used as keys in one dict would redact
# to one key and silently drop an entry. ``tools/import_dump.py`` hunts the
# same patterns on the way into ``dumps/`` and refuses a file where one
# survives, which is what catches a download taken before this line existed.
_ADDRESS_SHAPED = (
    re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
    re.compile(r"\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b"),
    re.compile(r"iNetX-[0-9A-Fa-f]{6}"),
)


def redact_addresses(value: Any) -> Any:
    """Replace anything address-shaped in any string, at any depth."""
    if isinstance(value, dict):
        return {key: redact_addresses(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_addresses(item) for item in value]
    if isinstance(value, str):
        for pattern in _ADDRESS_SHAPED:
            value = pattern.sub(REDACTED, value)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrumaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    The download is the evidence somebody pastes into an issue, so it is
    rendered the way the bus is read and quoted: every address in hex, and
    every value under the device that published it. json.dumps renders an int
    key as bare decimal -- 1539, not 0x0603 -- which then has to be converted
    by hand before it lines up with anything else in the report.

    Two passes of redaction, because they find different things. Key names
    cover the config entry, where the keys are Home Assistant's own and known.
    The address hunt covers everything, including the bus, where the keys are
    the panel's vocabulary and an address arrives as the value of one that says
    nothing about itself (#35).
    """
    coordinator = entry.runtime_data
    bus = coordinator.data
    bus_dict: dict[str, Any] | None = None
    if bus is not None:
        bus_dict = {
            "connected": bus.connected,
            # Whether startup has finished asking every device to describe
            # itself. A download taken before it has is a bus mid-discovery:
            # devices may still be nameless and parameters undescribed, which
            # is worth knowing before concluding anything from what is absent.
            "discovered": bus.discovered,
            "last_update": bus.last_update,
            "assigned_addr": f"0x{bus.assigned_addr:04X}",
            "devices": {
                f"0x{addr:04X}": asdict(bus.devices[addr]) | {
                    "addr": f"0x{addr:04X}",
                    # Not fields -- derived from the address and from
                    # Identify -- and exactly what a reader needs to tell two
                    # of a class apart without doing the arithmetic.
                    "cls": f"0x{bus.devices[addr].cls:02X}",
                    "instance": bus.devices[addr].instance,
                    "name": bus.devices[addr].name,
                    "serial": bus.devices[addr].serial,
                }
                for addr in sorted(bus.devices)
            },
            # The question a reader would otherwise have to work out by hand:
            # which topics have more than one device behind them on *this*
            # vehicle. Usually empty, which is itself worth saying out loud --
            # it is what made a single flat view look right for so long.
            "contested_topics": {
                topic: [f"0x{addr:04X}" for addr in addrs]
                for topic, addrs in bus.contested_topics().items()
            },
            # Values that named no source device. Nothing reads these; they
            # are here so a bus that somehow published everything this way
            # does not simply look empty.
            "unattributed": bus.unattributed,
        }
    return redact_addresses({
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        # Which kind of address this host connects on ("identity" or "rpa"),
        # learned from the first session that worked. Not identifying -- it
        # names a kind, not an address -- and it is what explains a host's
        # connect times (issue #13).
        "address_kind": coordinator.address_kind,
        # And which adapter the last session that came up ran over ("proxy" or
        # "local"). The pair is what tells a bond on one transport from a
        # session on the other, which is the open question in issue #13.
        "session_transport": coordinator.session_transport,
        # Whether the radio can ever hear a name: the panel's name rides a scan
        # response, an active scan is the only thing that solicits one, and an
        # adapter pinned passive never opens one. Without this, "the panel is
        # not discovered" is indistinguishable from "the panel is not there".
        "bluetooth_scanners": scan_mode_report(hass),
        "bluetooth_scanners_summary": describe_scan_modes(hass),
        "bus": bus_dict,
    })
