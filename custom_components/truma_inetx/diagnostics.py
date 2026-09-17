"""Diagnostics for Truma iNet X (BLE)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from .coordinator import TrumaConfigEntry

# The BLE address is a resolvable private address that still pins the panel to
# a location, and muid/uuid are the persisted app identity the panel bonds
# against. The bus state itself carries nothing identifying.
# discovery_keys is here because redaction works on key names and that
# subtree hides the address inside a value: Home Assistant serialises the key
# as ``"repr": "DiscoveryKey(domain='bluetooth', key='76:32:EF:78:FD:1A',
# version=1)"``, which no address-named key matches. Every download written
# before this line carried the panel's address in it, the three attached to
# issue #22 included.
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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrumaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    The download is the evidence somebody pastes into an issue, so it is
    rendered the way the bus is read and quoted: every address in hex, and
    every value under the device that published it. json.dumps renders an int
    key as bare decimal -- 1539, not 0x0603 -- which then has to be converted
    by hand before it lines up with anything else in the report.
    """
    coordinator = entry.runtime_data
    bus = coordinator.data
    bus_dict: dict[str, Any] | None = None
    if bus is not None:
        bus_dict = {
            "connected": bus.connected,
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
    return {
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
        "bus": bus_dict,
    }
