"""The Truma iNet X (BLE) integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .coordinator import TrumaConfigEntry, TrumaCoordinator

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Set up Truma iNet X from a config entry."""
    address: str = entry.data[CONF_ADDRESS].upper()

    # Not fatal: if the panel is not advertising right now the entities still
    # register (as unavailable) and the session task retries.
    if bluetooth.async_ble_device_from_address(hass, address, True) is None:
        LOGGER.warning(
            "Truma panel %s not currently reachable over BLE; entities will be "
            "unavailable until it is in range",
            address,
        )

    # A just-completed pairing hands off its live, encrypted connection here so
    # the session adopts it instead of reconnecting (which wedges the RPA).
    initial_client = hass.data.get(DOMAIN, {}).get("pending_clients", {}).pop(
        address, None
    )

    coordinator = TrumaCoordinator(hass, entry, address, initial_client=initial_client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
