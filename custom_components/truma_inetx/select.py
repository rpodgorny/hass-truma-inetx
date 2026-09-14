"""Select platform: every bus parameter the table presents as a choice."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaParamEntity, async_add_rows
from .profiles import Row

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0

OFF = "off"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma select entities.

    Each appears on the device that publishes its parameter. The supplemental
    electric element is an option, not standard: a Combi D has none and its
    panel does not describe EnergySrc.ElectricLevel at all, which is how that
    select came to be offering off / 900 W / 1800 W against hardware that can
    do none of them.
    """
    coordinator = entry.runtime_data
    async_add_rows(
        coordinator,
        async_add_entities,
        Platform.SELECT,
        lambda addr, topic, param, row: TrumaSelect(
            coordinator, addr, topic, param, row
        ),
    )


class TrumaSelect(TrumaParamEntity, SelectEntity):
    """One bus parameter, chosen from the values its device offers."""

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
        self._labels: dict[int, str] = dict(row.labels or {})
        self._values = {label: value for value, label in self._labels.items()}

    @property
    def options(self) -> list[str]:
        """The steps this device offers, in value order, plus any off.

        The device enumerates the parameter for the vehicle it is installed
        in, so it is the authority on which steps exist. Its *names* for them
        are not used: they arrive in the panel's display language, and the
        same three water steps come back as 40 / 60 / 70 on one vehicle and as
        Eco / Comfort / Hot on another (#12). Taking them as they come would
        make the option strings -- which automations match on -- differ per
        vehicle and per panel language.

        A device that describes nothing gets the whole table, which is what
        every vehicle was offered before the panel was asked.
        """
        values = self.device.allowed_values(self._topic, self._param)
        if values is None:
            values = list(self._labels)
        offered = [self._labels[value] for value in values if value in self._labels]
        if self.row.off_param is None:
            return offered
        return [OFF, *offered]

    @property
    def current_option(self) -> str | None:
        """The step currently selected, or off."""
        if self.row.off_param is not None:
            active = self.device.get(self._topic, self.row.off_param)
            if active == 0:
                return OFF
        value = self.value
        if not isinstance(value, int):
            return None
        return self._labels.get(value)

    async def async_select_option(self, option: str) -> None:
        """Select a step, switching the function on first where it has an off."""
        if option == OFF:
            assert self.row.off_param is not None
            await self.async_write(self.row.off_param, 0)
            return
        if self.row.off_param is not None:
            await self.async_write(self.row.off_param, 1)
        await self.async_write(self._param, self._values[option])
