"""Base entity for the Truma iNet X (BLE) integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import TrumaCoordinator
from .truma.state import TrumaState


class TrumaEntity(CoordinatorEntity[TrumaCoordinator]):
    """Common base tying entities to the coordinator and the device registry."""

    _attr_has_entity_name = True
    # When False the entity stays available even while the BLE link is down
    # (used by the connectivity sensor, which reports that link state itself).
    _gate_on_connected = True

    def __init__(self, coordinator: TrumaCoordinator, key: str) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        # Identity is the stable device name, never the rotating BLE address.
        self._attr_unique_id = f"{coordinator.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.unique_id)},
            name=coordinator.unique_id,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def data(self) -> TrumaState:
        """Shortcut to the current Truma state."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Entity is available only while the BLE link reports connected."""
        if not self._gate_on_connected:
            return super().available
        return super().available and self.coordinator.data.connected


@callback
def async_add_when_reported(
    coordinator: TrumaCoordinator,
    async_add_entities: Callable[[list[Entity]], None],
    pending: dict[str, Callable[[], Entity]],
) -> None:
    """Create each entity the first time its parameter is reported.

    Vehicles differ. A Combi and a panel are always there, but fresh and grey
    water tanks, a pump, gas-bottle sensors and a roof air conditioner are
    each present on some installations and absent on most. Creating their
    entities up front would give everyone else a row of permanently unknown
    values, and a value that is unknown because the hardware does not exist
    looks exactly like one that is unknown because the integration is broken.

    So the parameter arriving *is* the evidence the hardware exists. Since
    startup now asks every device on the bus for its values, that evidence
    lands within seconds of connecting rather than whenever the tank next
    happens to move.

    ``pending`` maps a ``Topic.Param`` key to a factory for the entity it
    justifies. Entities are added once and never removed: hardware that has
    answered once but is quiet now is still hardware, and deleting the entity
    would take its history with it.
    """

    @callback
    def _check() -> None:
        if not pending:
            return
        seen = coordinator.data.raw_params
        ready = [key for key in pending if key in seen]
        if not ready:
            return
        async_add_entities([pending.pop(key)() for key in ready])

    # Data may already be in hand -- a reload of the config entry re-runs
    # platform setup against a coordinator that is already connected.
    _check()
    if pending:
        unsub = coordinator.async_add_listener(_check)
        coordinator.config_entry.async_on_unload(unsub)
