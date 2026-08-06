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
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "state": asdict(state) if state is not None else None,
    }
