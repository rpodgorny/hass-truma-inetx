"""Base entity for the Truma iNet X (BLE) integration."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bus import Bus, Device
from .coordinator import TrumaCoordinator
from .profiles import Row, rows_for


class TrumaEntity(CoordinatorEntity[TrumaCoordinator]):
    """Common base tying an entity to one device on the panel's bus."""

    _attr_has_entity_name = True
    # When False the entity stays available even while the BLE link is down
    # (used by the connectivity sensor, which reports that link state itself).
    _gate_on_connected = True

    def __init__(self, coordinator: TrumaCoordinator, addr: int, key: str) -> None:
        """Initialize an entity belonging to the device at ``addr``."""
        super().__init__(coordinator)
        self._addr = addr
        # The bus address, not the device's serial number.
        #
        # The serial would survive a re-pairing, which renumbers addresses, and
        # that is the argument for it. But it is not always there when an
        # entity is created: a device that first speaks through a plain change
        # notification rather than through a discovery answer has published one
        # parameter and no Identify, so its identity would be the address that
        # time and the serial the next -- and a unique_id that differs between
        # restarts orphans the entity and takes its history with it. A restart
        # happens daily; a re-pairing is a deliberate act, and the serial is on
        # the device page either way.
        self._attr_unique_id = f"{coordinator.unique_id}_{addr:04X}_{key}"
        self._attr_device_info = coordinator.device_info(addr)

    @property
    def bus(self) -> Bus:
        """Shortcut to the current bus state."""
        return self.coordinator.data

    @property
    def device(self) -> Device:
        """The bus device this entity belongs to."""
        return self.bus.device(self._addr)

    @property
    def available(self) -> bool:
        """Entity is available only while the BLE link reports connected."""
        if not self._gate_on_connected:
            return super().available
        return super().available and self.bus.connected


class TrumaParamEntity(TrumaEntity):
    """An entity built from one presentation row for one bus parameter."""

    def __init__(
        self,
        coordinator: TrumaCoordinator,
        addr: int,
        topic: str,
        param: str,
        row: Row,
    ) -> None:
        """Initialize from the row the parameter is presented as."""
        super().__init__(coordinator, addr, f"{topic}.{param}_{row.translation_key}")
        self._topic = topic
        self._param = param
        self.row = row
        self._attr_translation_key = row.translation_key
        self._attr_entity_category = row.entity_category
        self._attr_entity_registry_enabled_default = row.enabled_default
        if row.placeholders is not None:
            self._attr_translation_placeholders = row.placeholders(param)

    @property
    def value(self) -> object:
        """The raw wire value this device last published for the parameter."""
        return self.device.get(self._topic, self._param)

    async def async_write(self, param: str, value: int) -> None:
        """Write a parameter of this entity's own topic, to its own device."""
        await self.coordinator.async_write(self._addr, self._topic, param, value)


@callback
def async_add_rows(
    coordinator: TrumaCoordinator,
    async_add_entities: Callable[[list[Entity]], None],
    platform: Platform,
    build: Callable[[int, str, str, Row], Entity],
) -> None:
    """Create an entity for every device × parameter the table has a row for.

    Vehicles differ, and so do buses. A Combi and a panel are always there,
    but fresh and grey water tanks, a pump, gas-bottle sensors, an electrical
    block and a roof air conditioner are each present on some installations
    and absent on most. Creating their entities up front would give everyone
    else a row of permanently unknown values, and a value that is unknown
    because the hardware does not exist looks exactly like one that is unknown
    because the integration is broken.

    So the parameter arriving *is* the evidence the hardware exists -- and
    which device published it is the evidence of where it lives. Since startup
    asks every device on the bus for its values, that evidence lands within
    seconds of connecting rather than whenever the tank next happens to move.

    Entities are added once and never removed: hardware that has answered once
    but is quiet now is still hardware, and deleting the entity would take its
    history with it. A device that appears later simply gets its entities
    then, which is how a battery-powered gas sensor that takes minutes to wake
    up is handled without waiting for it at startup.

    A row that opts into ``requires_avail`` asks for one thing more: that the
    device stop saying the parameter is unavailable. That is for the fixed-size
    lists a device publishes whole -- a panel has six timer slots whether or
    not it has six timers, and publishes all six, marking the empty ones
    ``avail`` 0. Waiting for the flag is still waiting for evidence; it is just
    the evidence that this copy of the parameter means something.
    """
    made: set[tuple[int, str, str, str]] = set()

    @callback
    def _check() -> None:
        new: list[Entity] = []
        for addr, device in list(coordinator.data.devices.items()):
            if not coordinator.device_is_named(addr):
                # Not recorded as made: the entity is built at the update that
                # settles the device's name. Home Assistant mints an entity id
                # from the device name at creation and never revises it, so
                # building now would stamp "bus_device_0x0201" into it for
                # good -- see TrumaCoordinator.device_is_named.
                continue
            for key in list(device.params):
                topic, _, param = key.partition(".")
                for row in rows_for(topic, param, platform):
                    ident = (addr, topic, param, row.translation_key)
                    if ident in made:
                        continue
                    if row.requires_avail and device.meta(topic, param).get(
                        "avail"
                    ) == 0:
                        # Only an explicit 0 withholds it. A device that says
                        # nothing about availability gets the entity, the way
                        # it gets a control it said nothing about writing --
                        # a control withheld on a guess is invisible, and one
                        # offered against absent hardware gets reported.
                        #
                        # Not recorded as made: a slot filled later is built
                        # at the update that fills it.
                        continue
                    made.add(ident)
                    new.append(build(addr, topic, param, row))
        if new:
            async_add_entities(new)

    # Data may already be in hand -- a reload of the config entry re-runs
    # platform setup against a coordinator that is already connected.
    _check()
    unsub = coordinator.async_add_listener(_check)
    coordinator.config_entry.async_on_unload(unsub)


@callback
def async_add_per_device(
    coordinator: TrumaCoordinator,
    async_add_entities: Callable[[list[Entity]], None],
    topic: str,
    param: str,
    build: Callable[[int], Entity],
) -> None:
    """Create one entity per device that publishes a given parameter.

    For the entities the table cannot describe because they are not one
    parameter: the climate entity is a mode, a setpoint, a reading and a fan
    speed at once, and which device owns it is still answered the same way --
    by which one publishes its defining parameter.
    """
    made: set[int] = set()

    @callback
    def _check() -> None:
        new: list[Entity] = []
        for addr, device in list(coordinator.data.devices.items()):
            if addr in made or not device.reports(topic, param):
                continue
            # Same wait as async_add_rows, and this is the path the climate
            # entity is built on -- climate.bus_device_0x0201 is what it cost
            # on the vehicle of #23.
            if not coordinator.device_is_named(addr):
                continue
            made.add(addr)
            new.append(build(addr))
        if new:
            async_add_entities(new)

    _check()
    unsub = coordinator.async_add_listener(_check)
    coordinator.config_entry.async_on_unload(unsub)
