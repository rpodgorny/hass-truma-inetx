"""Select platform for Truma iNet X water and electric heating modes."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, async_add_when_reported

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0

WATER_OFF = "off"
# Both halves on purpose (#12). A panel that shows Eco / Comfort / Hot and a
# panel that shows 40 / 60 / 70 are the same three steps, so the name matches
# what is written on the vehicle and the temperature says what the name means
# to anyone whose panel does not use words. Our own text, not the panel's --
# see _offered.
_WATER_MODE_TO_LABEL = {0: "Eco (40 °C)", 1: "Comfort (60 °C)", 2: "Hot (70 °C)"}
WATER_OPTIONS = {WATER_OFF: None} | {
    label: value for value, label in _WATER_MODE_TO_LABEL.items()
}

_ELECTRIC_VALUE_TO_LABEL = {0: "off", 1: "900 W", 2: "1800 W"}
ELECTRIC_OPTIONS = {label: value for value, label in _ELECTRIC_VALUE_TO_LABEL.items()}


def _offered(state, topic: str, param: str, labels: dict) -> list:
    """The labels for the values this panel offers, in value order.

    The panel enumerates each parameter for the vehicle it is installed in, so
    it is the authority on which steps exist. Its *names* for them are not
    used: they arrive in the panel's display language, and the same three water
    steps come back as ``40 / 60 / 70`` on one vehicle and as Eco / Comfort /
    Hot on another (#12). Taking them as they come would make the option
    strings -- which automations match on -- differ per vehicle and per panel
    language. So the labels stay ours and carry both halves; only which of them
    to show is the panel's call.

    A panel that describes nothing gets the full list, which is what every
    vehicle was offered before this existed.
    """
    values = state.allowed_values(topic, param)
    if values is None:
        values = list(labels)
    return [labels[value] for value in values if value in labels]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma select entities."""
    coordinator = entry.runtime_data
    async_add_entities([TrumaWaterModeSelect(coordinator)])
    # The supplemental electric element is an option, not standard: a Combi D
    # has none, and its panel does not describe EnergySrc.ElectricLevel at all
    # (measured on a Combi D van, whose select was offering off / 900 W /
    # 1800 W against hardware that cannot do any of them). The parameter
    # arriving is the evidence the element exists.
    async_add_when_reported(
        coordinator,
        async_add_entities,
        {"EnergySrc.ElectricLevel": lambda: TrumaElectricLevelSelect(coordinator)},
    )


class TrumaWaterModeSelect(TrumaEntity, SelectEntity):
    """Water heating mode (off / Eco / Comfort / Hot)."""

    _attr_translation_key = "water_mode"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "water_mode")

    @property
    def options(self) -> list[str]:
        """Off, plus the temperature steps this panel offers."""
        return [
            WATER_OFF,
            *_offered(self.data, "WaterHeating", "Mode", _WATER_MODE_TO_LABEL),
        ]

    @property
    def current_option(self) -> str | None:
        """Return the current water heating mode."""
        if self.data.water_active == 0:
            return WATER_OFF
        if self.data.water_mode is None:
            return None
        return _WATER_MODE_TO_LABEL.get(self.data.water_mode)

    async def async_select_option(self, option: str) -> None:
        """Set the water heating mode."""
        if option == WATER_OFF:
            await self.coordinator.async_write("WaterHeating", "Active", 0)
            return
        await self.coordinator.async_write("WaterHeating", "Active", 1)
        await self.coordinator.async_write("WaterHeating", "Mode", WATER_OPTIONS[option])


class TrumaElectricLevelSelect(TrumaEntity, SelectEntity):
    """Supplemental electric heating level (off / 900 / 1800 W)."""

    _attr_translation_key = "electric_level"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "electric_level")

    @property
    def options(self) -> list[str]:
        """The electric steps this panel offers.

        A heater without the electric element still has the parameter; its
        panel is the one that says which levels mean anything on it.
        """
        return _offered(
            self.data, "EnergySrc", "ElectricLevel", _ELECTRIC_VALUE_TO_LABEL
        )

    @property
    def current_option(self) -> str | None:
        """Return the current electric heating level."""
        if self.data.electric_level is None:
            return None
        return _ELECTRIC_VALUE_TO_LABEL.get(self.data.electric_level)

    async def async_select_option(self, option: str) -> None:
        """Set the electric heating level."""
        await self.coordinator.async_write(
            "EnergySrc", "ElectricLevel", ELECTRIC_OPTIONS[option]
        )
