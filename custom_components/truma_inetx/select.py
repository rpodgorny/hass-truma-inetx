"""Select platform for Truma iNet X water and electric heating modes."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity

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

_ELECTRIC_VALUE_TO_LABEL = {1: "900 W", 2: "1800 W"}
ELECTRIC_OPTIONS = {label: value for value, label in _ELECTRIC_VALUE_TO_LABEL.items()}

ENERGY_DIESEL = "diesel"
ENERGY_ELECTRIC = "electric"
ENERGY_HYBRID = "hybrid"
ENERGY_OPTIONS = [ENERGY_DIESEL, ENERGY_ELECTRIC, ENERGY_HYBRID]
_ENERGY_SOURCE_PARAMS = {
    "EnergySrc.DieselLevel", "EnergySrc.GasLevel", "EnergySrc.ElectricLevel"
}


def _reported_sources(state) -> set[str]:
    """Hardware presence is independent of whether a source is active."""
    return _ENERGY_SOURCE_PARAMS.intersection(state.raw_params)


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
    added = False

    @callback
    def add_energy_controls() -> None:
        nonlocal added
        if added or not _reported_sources(coordinator.data):
            return
        added = True
        async_add_entities([
            TrumaEnergySourceSelect(coordinator), TrumaElectricLevelSelect(coordinator)
        ])

    add_energy_controls()
    if not added:
        coordinator.config_entry.async_on_unload(
            coordinator.async_add_listener(add_energy_controls)
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
    """Electric output for multiple sources, including gas/electric on/off."""

    _attr_translation_key = "electric_level"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "electric_level")

    @property
    def _has_combined_source(self) -> bool:
        """Use the same hardware evidence as the combined source selector."""
        return {"EnergySrc.DieselLevel", "EnergySrc.ElectricLevel"}.issubset(
            self.data.raw_params
        )

    @property
    def options(self) -> list[str]:
        """Offer only panel-declared steps when electric source choice exists."""
        if not self._has_electric_choice:
            return []
        labels = _ELECTRIC_VALUE_TO_LABEL if self._has_combined_source else {
            0: "off", **_ELECTRIC_VALUE_TO_LABEL
        }
        return _offered(
            self.data, "EnergySrc", "ElectricLevel", labels
        )

    @property
    def available(self) -> bool:
        """Require multiple sources; gas/electric remains controllable at zero."""
        return super().available and self._hardware_enabled

    @property
    def _has_electric_choice(self) -> bool:
        sources = _reported_sources(self.data)
        return len(sources) >= 2 and "EnergySrc.ElectricLevel" in sources

    @property
    def _hardware_enabled(self) -> bool:
        return self._has_electric_choice and (
            not self._has_combined_source or bool(self.data.electric_level)
        )

    @property
    def current_option(self) -> str | None:
        """Return the current electric heating level."""
        if not self._has_electric_choice:
            return None
        if self.data.electric_level == 0:
            return None if self._has_combined_source else "off"
        return _ELECTRIC_VALUE_TO_LABEL.get(self.data.electric_level)

    async def async_select_option(self, option: str) -> None:
        """Set the electric heating level."""
        if not self._hardware_enabled:
            raise HomeAssistantError("Electric heating control is disabled for the reported sources")
        if option not in self.options:
            raise HomeAssistantError(f"Unsupported electric heating level: {option}")
        if option == "off":
            await self.coordinator.async_write("EnergySrc", "ElectricLevel", 0)
            return
        await self.coordinator.async_write(
            "EnergySrc", "ElectricLevel", ELECTRIC_OPTIONS[option]
        )


class TrumaEnergySourceSelect(TrumaEntity, SelectEntity):
    """Coordinate supported diesel/electric hardware; otherwise stay disabled."""

    _attr_translation_key = "energy_source"
    _attr_options = ENERGY_OPTIONS
    _attr_icon = "mdi:engine"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "energy_source")

    @property
    def options(self) -> list[str]:
        """Adapt one entity as the panel reports its installed hardware."""
        if not self._has_combined_source:
            return []
        return [*ENERGY_OPTIONS, "changing"] if self._changing else ENERGY_OPTIONS

    @property
    def _has_combined_source(self) -> bool:
        return {"EnergySrc.DieselLevel", "EnergySrc.ElectricLevel"}.issubset(
            self.data.raw_params
        )

    @property
    def available(self) -> bool:
        return super().available and self._has_combined_source

    @property
    def _changing(self) -> bool:
        return getattr(self.coordinator, "energy_source_changing", False)

    @property
    def current_option(self) -> str | None:
        """Derive the user-facing source from both hardware levels."""
        if not self._has_combined_source:
            return None
        if self._changing:
            return "changing"
        diesel = self.data.diesel_level
        electric = self.data.electric_level
        if diesel is None or electric is None:
            return None
        if diesel and electric:
            return ENERGY_HYBRID
        if diesel:
            return ENERGY_DIESEL
        if electric:
            return ENERGY_ELECTRIC
        return None

    async def async_select_option(self, option: str) -> None:
        """Apply a source safely, always entering electric modes at 900 W."""
        if not self._has_combined_source:
            raise HomeAssistantError("Energy source control requires reported diesel and electric sources")
        if option == "changing":
            raise HomeAssistantError("Changing is a status, not an energy source")
        if option not in self.options:
            raise HomeAssistantError(f"Unsupported energy source: {option}")
        if option == ENERGY_DIESEL:
            commands = [
                ("EnergySrc", "DieselLevel", 1),
                ("EnergySrc", "ElectricLevel", 0),
            ]
        elif option == ENERGY_ELECTRIC:
            commands = [
                ("EnergySrc", "ElectricLevel", 1),
                ("EnergySrc", "DieselLevel", 0),
            ]
        elif option == ENERGY_HYBRID:
            commands = [
                ("EnergySrc", "DieselLevel", 1),
                ("EnergySrc", "ElectricLevel", 1),
            ]
        else:
            raise ValueError(f"Unknown energy source: {option}")
        await self.coordinator.async_write_many(
            commands, confirm=True, action="energy_source", target=option
        )
        # Only after fresh device feedback confirmed the complete setting.
        # Entities not yet attached to HA cannot create a device activity entry.
        if (hass := getattr(self, "hass", None)) is not None:
            german = hass.config.language.startswith("de")
            label = {"diesel": "Diesel", "electric": "Elektro" if german else "Electric", "hybrid": "Hybrid"}[option]
            message = (
                f"Von der Truma bestätigt: Energiequelle {label}"
                if german else f"Confirmed by Truma: energy source {label}"
            )
            hass.bus.async_fire("logbook_entry", {
                "name": "Truma", "message": message,
                "entity_id": self.entity_id, "domain": "select",
            })
