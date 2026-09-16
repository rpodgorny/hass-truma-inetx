"""Number platform: every bus parameter the table presents as a slider."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_rows
from .profiles import Row

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Truma numbers."""
    coordinator = entry.runtime_data
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.NUMBER,
        lambda addr, topic, param, row: TrumaNumber(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaNumber(TrumaParamEntity, NumberEntity):
    """One bus parameter, written within its own device's range."""

    _attr_mode = NumberMode.SLIDER

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
        self._attr_native_unit_of_measurement = row.unit
        self._attr_native_step = row.step

    def _bounds(self) -> tuple[float, float]:
        """The device's own range, or the row's fallback.

        The circulation fan used to be 0-10 for every device that published
        it, which is a Combi's range handed to a roof air conditioner as well.
        The device that owns the parameter describes its own limits, and where
        it has, they win.
        """
        described = self.device.bounds(self._topic, self._param)
        if described is None:
            return self.row.fallback_bounds or (0, 100)
        limit = self.row.bounds_limit
        if limit is None:
            return described
        # A description that is the width of the field rather than a range of
        # values -- the panel's display timeout is 0 to 4294967295 seconds --
        # is clipped into what the row says is worth offering. It still only
        # ever narrows: a device that describes less than the limit keeps its
        # own answer.
        return (max(described[0], limit[0]), min(described[1], limit[1]))

    @property
    def native_min_value(self) -> float:
        """Lowest value this device accepts."""
        return self._bounds()[0]

    @property
    def native_max_value(self) -> float:
        """Highest value this device accepts."""
        return self._bounds()[1]

    @property
    def native_value(self) -> float | None:
        """The device's current value."""
        value = self.value
        if not isinstance(value, (int, float)):
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device that owns the parameter."""
        await self.async_write(self._param, int(value))
