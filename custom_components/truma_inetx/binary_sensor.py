"""Binary sensor platform for Truma iNet X flame and link status."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrumaConfigEntry, TrumaCoordinator
from .entity import TrumaEntity, async_add_when_reported
from .truma.state import ActiveState

# Entities are coordinator-driven and have no update() method, so Home
# Assistant would create no semaphore anyway; stated explicitly.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrumaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Truma binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            TrumaFlameSensor(coordinator),
            TrumaConnectionSensor(coordinator),
            TrumaProxySensor(coordinator),
        ]
    )
    # Gas appears only on a heater that burns it, and then only to be read.
    async_add_when_reported(
        coordinator,
        async_add_entities,
        {"EnergySrc.GasLevel": lambda: TrumaGasSensor(coordinator)},
    )


class TrumaFlameSensor(TrumaEntity, BinarySensorEntity):
    """Flame/burner running status."""

    _attr_translation_key = "flame"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "flame")

    @property
    def is_on(self) -> bool | None:
        """Whether the burner is actually firing.

        ``System.FlameStatus`` is type 105, the family the various ``Active``
        parameters belong to, and it takes three values: 0 off, 1 running,
        2 idle. Measured on a Combi 6 E against an independent shore-power
        meter (#15) -- the value went 1 -> 2 in the same second the draw fell
        from 1787 W to 105 W, with ``AirHeating.Active`` making the same move
        in the same record.

        So "anything above zero" is not on. 2 is the appliance standing by,
        and reporting a flame while it stands by is worse than reporting
        nothing at all: it is the reading an automation would act on.

        It does not name the energy source either, which was the other
        candidate reading: on the one vehicle reported that has both, it read
        1 with gas off and a 1775 W element running.
        """
        if self.data.flame_status is None:
            return None
        return self.data.flame_status == ActiveState.ACTIVE


class TrumaGasSensor(TrumaEntity, BinarySensorEntity):
    """Whether the heater is drawing on gas.

    Deliberately a sensor and not a switch (#16). ``EnergySrc.GasLevel`` is
    writable and a write to it does go through -- measured on a gas/electric
    Combi 6 E, where the panel followed and the value came back. But the heater
    writes it too: on that vehicle ``NeedsEnergySrc`` read 1 throughout, and
    switching the electric element off moved the gas source on by itself, with
    nothing sent from here. A switch presented as the user's to own would
    therefore fight the heater and flap, so this reflects the heater's choice
    instead of pretending to make it.

    Gas is what a Combi burns when it has no diesel burner, so the parameter
    arriving is also the evidence this is a gas heater -- see the diesel switch
    in ``switch.py``, which waits on its own parameter for the same reason.
    """

    _attr_translation_key = "gas"

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "gas")

    @property
    def is_on(self) -> bool | None:
        """Whether gas is selected as an energy source."""
        if self.data.gas_level is None:
            return None
        return bool(self.data.gas_level)


class TrumaConnectionSensor(TrumaEntity, BinarySensorEntity):
    """BLE link status to the panel."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = None
    _gate_on_connected = False  # reports the link itself → never gated

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "connection")

    @property
    def is_on(self) -> bool:
        """Whether the BLE link to the panel is up."""
        return self.coordinator.panel_link_connected


class TrumaProxySensor(TrumaEntity, BinarySensorEntity):
    """Availability of the ESPHome proxy used for this panel."""

    _attr_translation_key = "proxy_connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = None
    _gate_on_connected = False

    def __init__(self, coordinator: TrumaCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "proxy_connection")

    @property
    def is_on(self) -> bool | None:
        """Whether the identified Bluetooth proxy is registered in HA."""
        return self.coordinator.proxy_available
