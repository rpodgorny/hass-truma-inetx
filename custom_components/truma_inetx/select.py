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
        # Every option resolves through here, to the parameter and the value
        # that selects it. The row's own labels are laid down first, so a
        # device that enumerates its own off keeps it: EnergySrc.ElectricLevel
        # names 0 "Electric off" on a gas/electric Combi, and writing that 0
        # is what switches the element off (#28).
        self._writes: dict[str, tuple[str, int]] = {
            label: (param, value) for value, label in self._labels.items()
        }
        # An off the parameter itself has no value for. Water heating is
        # switched off by WaterHeating.Active while WaterHeating.Mode
        # enumerates the three temperature steps and nothing else, so that
        # option is ours to invent -- and it goes in beside the rest rather
        # than being recognised by its spelling on the way back in. Home
        # Assistant hands a select one flat list of strings and hands the same
        # strings back, so an invented option and a label are the same kind of
        # thing by the time it returns; a sentinel that outranked the labels
        # is how off became unselectable on the one vehicle whose own enum
        # offered it (#28).
        self._off: str | None = None
        if row.off_param is not None and OFF not in self._writes:
            self._off = OFF
            self._writes[OFF] = (row.off_param, 0)

    @property
    def options(self) -> list[str]:
        """The steps this device offers, in value order, plus any invented off.

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
        if self._off is None:
            return offered
        return [self._off, *offered]

    @property
    def current_option(self) -> str | None:
        """The step currently selected, or off."""
        if self._off is not None:
            off_param, _ = self._writes[self._off]
            if self.device.get(self._topic, off_param) == 0:
                return self._off
        value = self.value
        if not isinstance(value, int):
            return None
        return self._labels.get(value)

    async def async_select_option(self, option: str) -> None:
        """Select a step, switching the function on first where it has an off."""
        param, value = self._writes[option]
        off_param = self.row.off_param
        if off_param is not None and param == self._param:
            # A step is being chosen, and this function is switched off in its
            # own right: switch it on before saying which step. Selecting the
            # invented off writes off_param itself, and nothing else.
            await self.async_write(off_param, 1)
        await self.async_write(param, value)
