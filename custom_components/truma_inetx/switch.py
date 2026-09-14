"""Switch platform: every bus parameter the table presents as a switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up the Truma switches.

    None of them is universal. A gas/electric Combi has no diesel burner and
    its panel never mentions EnergySrc.DieselLevel (#16); only a vehicle with
    a water system has a pump; and the two water-priority modes are not even a
    pair that arrives together. Each appears on the device that publishes its
    parameter and nowhere else -- see ``async_add_rows``.
    """
    coordinator = entry.runtime_data
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.SWITCH,
        lambda addr, topic, param, row: TrumaSwitch(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaSwitch(TrumaParamEntity, SwitchEntity):
    """One bus parameter, written as 0 or 1."""

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

    @property
    def is_on(self) -> bool | None:
        """Whether the parameter is set."""
        value = self.value
        if not isinstance(value, (int, float)):
            return None
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set the parameter."""
        await self.async_write(self._param, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the parameter."""
        await self.async_write(self._param, 0)
