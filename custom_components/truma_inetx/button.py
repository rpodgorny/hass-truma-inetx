"""Button controls for immediate Truma BLE sessions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the manual-session buttons."""
    async_add_entities(
        [
            TrumaManualSyncButton(entry.runtime_data),
            TrumaManualStopButton(entry.runtime_data),
        ]
    )


class TrumaManualSyncButton(TrumaEntity, ButtonEntity):
    """Wake BLE now and start the configured live-mode duration."""

    _attr_translation_key = "manual_sync"
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "manual_sync")

    async def async_press(self) -> None:
        """Request the manual session through the coordinator."""
        await self.coordinator.async_request_manual_session(
            self.coordinator.manual_live_minutes
        )


class TrumaManualStopButton(TrumaEntity, ButtonEntity):
    """End a timed live session early."""

    _attr_translation_key = "manual_stop"
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "manual_stop")

    async def async_press(self) -> None:
        """Release the timed hold through the coordinator."""
        await self.coordinator.async_end_manual_session()
