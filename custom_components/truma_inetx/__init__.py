"""The Truma iNet X (BLE) integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

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

    async def _async_stop(_event: Event) -> None:
        """Close the BLE link before Home Assistant exits."""
        # Logged so a later wedge can be told apart at a glance: this line
        # present means we closed the link and the panel should have freed its
        # slot; absent means the link was dropped by the process exiting.
        LOGGER.debug("Truma %s: Home Assistant stopping, closing the BLE link", address)
        await coordinator.async_stop()

    # Home Assistant does NOT unload config entries when it shuts down — on
    # EVENT_HOMEASSISTANT_STOP it only calls ConfigEntry.async_shutdown(), so
    # async_unload_entry below runs on reload/removal but never on a restart.
    # Without this listener the process exits with the BLE link still open: the
    # panel never sees a disconnect, waits out its supervision timeout, and can
    # keep the session (and one of its ~4 connection slots) allocated. It then
    # answers the next connect with ESP_GATT_CONN_FAIL_ESTABLISH until it is
    # power-cycled. Disconnecting while HA is still alive sends a proper
    # link-layer terminate instead, so the panel frees the slot immediately.
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TrumaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
