"""Sensor platform: every bus parameter the table presents as a sensor."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import LOGGER
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
        if row.labels is not None:
            self._attr_options = list(row.labels.values())
        self._reported_unpresentable = False
        self._reported_unnamed: set[str] = set()

    @property
    def extra_state_attributes(self) -> dict | None:
        """What the row carries beside its state, if it carries anything.

        For a parameter whose wire value is a structure: the state is the one
        number worth looking up, and the rest of what the appliance said about
        it belongs beside that number rather than in a second entity nobody
        would think to look at.
        """
        if self.row.attrs is None:
            return None
        return self.row.attrs(self.value) or None

    # Deliberately not a property that hides an empty result: a row that
    # builds attributes at all returns the same keys every time it has a
    # value to build them from, so that what reads them never has to ask
    # which shape it got.

    def _named(self, value: object) -> str | None:
        """The row's name for a value the device sent, or nothing.

        An ENUM sensor may only ever be in one of the states it declared:
        Home Assistant rejects anything else from inside the state write,
        which is once per coordinator update forever. So a value the row has
        no name for reads unknown -- and it is still in the attributes, where
        whoever goes looking for the fourth state can see the number that
        produced it.
        """
        assert self.row.labels is not None
        if isinstance(value, int) and not isinstance(value, bool):
            named = self.row.labels.get(value)
            if named is not None:
                return named
        # Keyed by repr, because the value that turns up unnamed is not
        # necessarily one a set will take -- the parameter that taught us to
        # expect the unexpected here was a list.
        seen = repr(value)
        if value is not None and seen not in self._reported_unnamed:
            self._reported_unnamed.add(seen)
            LOGGER.warning(
                "Truma %s.%s at 0x%04X reported %r, which the %s row has no "
                "name for; the sensor reads unknown and carries the value in "
                "its attributes. Please report it -- named values are %s",
                self._topic,
                self._param,
                self._addr,
                value,
                self.row.translation_key,
                sorted(int(key) for key in self.row.labels),
            )
        return None

    @property
    def native_value(self) -> float | int | str | datetime | None:
        """The device's own value for the parameter, in the row's unit."""
        value = self.value
        if value is None:
            return None
        value = native(self.row, value)
        if self.row.labels is not None:
            return self._named(value)
        # datetime among them for the TIMESTAMP device class, which is the
        # one that wants an object rather than a number: a row carrying a
        # wire epoch reduces it to an aware datetime and Home Assistant
        # renders it in the user's own timezone.
        if value is None or isinstance(value, (int, float, str, datetime)):
            return value  # type: ignore[return-value]

        # A parameter whose wire value is a structure, presented by a row that
        # does not reduce it to one value. Home Assistant raises on that from
        # inside the state write, which means once per coordinator update
        # forever -- measured on the van, where NrFreeSlots turned out to be a
        # list of per-device-kind counts and filled the log with tracebacks.
        # The reading is missing either way; this makes it missing quietly,
        # and says which row to go and fix.
        if not self._reported_unpresentable:
            self._reported_unpresentable = True
            LOGGER.warning(
                "Truma %s.%s at 0x%04X is %s, which the %s row presents as a "
                "single value; the sensor will read unknown until the row "
                "reduces it",
                self._topic,
                self._param,
                self._addr,
                type(value).__name__,
                self.row.translation_key,
            )
        return None
