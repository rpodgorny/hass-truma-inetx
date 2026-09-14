"""Sensor platform: every bus parameter the table presents as a sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_rows
from .profiles import Row, native

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma sensors."""
    coordinator = entry.runtime_data
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.SENSOR,
        lambda addr, topic, param, row: TrumaSensor(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaSensor(TrumaParamEntity, SensorEntity):
    """One bus parameter, read."""

    def __init__(
        self,
        coordinator: TrumaCoordinator,
        addr: int,
        topic: str,
        param: str,
        row: Row,
    ) -> None:
        """Initialize from the row."""
        super().__init__(coordinator, addr, topic, param, row)
        self._attr_device_class = row.device_class
        self._attr_state_class = row.state_class
        self._attr_native_unit_of_measurement = row.unit
        self._attr_suggested_display_precision = row.precision

    @property
    def native_value(self) -> float | int | str | None:
        """The device's own value for the parameter, in the row's unit."""
        value = self.value
        if value is None:
            return None
        return native(self.row, value)  # type: ignore[return-value]
