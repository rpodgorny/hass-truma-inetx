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
# against. The panel state itself carries nothing identifying.
TO_REDACT = {
    CONF_ADDRESS,
    CONF_NAME,
    "title",
    "unique_id",
    "address",
    "muid",
    "uuid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrumaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    state = coordinator.data
    state_dict = asdict(state) if state is not None else None
    if state_dict is not None:
        # asdict leaves seen_devices a set, which does not survive the JSON
        # dump. Hex is also how these addresses are read: a download is the
        # evidence for what is actually on someone's bus.
        state_dict["seen_devices"] = [
            f"0x{addr:04X}" for addr in sorted(state_dict["seen_devices"])
        ]
        # device_params is keyed by those same addresses, and json.dumps
        # renders an int key as bare decimal -- 1539, not 0x0603 -- which then
        # has to be converted by hand to line up against seen_devices,
        # topic_source, or anything quoted in an issue. Same form, same reason.
        state_dict["device_params"] = {
            f"0x{addr:04X}": params
            for addr, params in sorted(state_dict["device_params"].items())
        }
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "state": state_dict,
    }
