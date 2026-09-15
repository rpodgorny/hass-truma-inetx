#!/usr/bin/env python3
"""Offline checks for the panel's own Bluetooth side, at bus address 0x0601.

No hardware and no Home Assistant install: HA is stubbed and the real
``bus.py``, ``profiles.py`` and ``sensor.py`` are loaded against it, then the
entities are constructed and read.

Why this exists: a session can wedge with the link up by everything the host
reports and nothing carried over it, and the panel that could say why has
stopped answering by then -- so it cannot be asked during one. 0x0601
publishes the panel's own view of that link, and measured on the van it is the
only address on the bus that publishes no appliance parameter at all: seven
``BleDeviceManagement`` parameters, three ``DeviceManagement`` ones, and no
``Identify.Name``.

What it pins:

1. the three sensors are created on the device that published them, and only
   once it has,
2. the free-slot count is a measurement and the two state codes are not --
   they are codes, and a long-term average of a code is a number no device
   ever reported,
3. all three are diagnostics and all three are on by default -- a reading
   only worth having before the failure it explains cannot wait for somebody
   to go and enable it,
4. none of them is offered as a named state, because the panel describes none
   of them with an enum,
5. the free-slot count is summed out of the breakdown by device kind the
   parameter actually carries,
6. and a structured value a row does not reduce reads unknown rather than
   raising inside Home Assistant once per coordinator update.

Run: ``python3 tests/test_ble_mgmt_entities.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

# The panel's Bluetooth management, as measured on the van. The heater is
# named beside it only to show a value from elsewhere does not land here.
BLE_MGMT = 0x0601
HEATER = 0x0201

stubs.install_homeassistant()
BUS = stubs.load("bus")
stubs.load("const")
stubs.mod("truma_pkg.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)
PROFILES = stubs.load("profiles")
stubs.load("entity")
SENSOR = stubs.load("sensor")

KEYS = ("ble_free_slots", "ble_conn_state", "ble_mgmt_state")


def _coordinator() -> stubs.FakeCoordinator:
    return stubs.FakeCoordinator(BUS.Bus())


def _keys(entities) -> list:
    return [getattr(entity, "_attr_translation_key", None) for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if getattr(entity, "_attr_translation_key", None) == key:
            return entity
    raise AssertionError(f"no entity with translation key {key}")


# What the parameter actually carries, measured on the van: a free-slot count
# per kind of device rather than the single number its name suggests.
FREE_SLOTS = [{"type": 12, "nrOfSlots": 1}, {"type": 9, "nrOfSlots": 2}]


def test_they_appear_on_the_device_that_published_them() -> None:
    """And not before: an address that has said nothing has no entities."""
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    assert not set(KEYS) & set(_keys(made)), made

    coordinator.report("BleDeviceManagement", "NrFreeSlots", FREE_SLOTS, BLE_MGMT)
    coordinator.report("BleDeviceManagement", "BleConnState", 3, BLE_MGMT)
    coordinator.report("BleDeviceManagement", "State", 1, BLE_MGMT)

    for key in KEYS:
        assert _by_key(made, key)._addr == BLE_MGMT, key
    assert _by_key(made, "ble_free_slots").native_value == 3
    assert _by_key(made, "ble_conn_state").native_value == 3
    assert _by_key(made, "ble_mgmt_state").native_value == 1

    # ...and once each, however many frames follow.
    before = len(made)
    coordinator.report(
        "BleDeviceManagement", "NrFreeSlots", [{"type": 12, "nrOfSlots": 0}], BLE_MGMT
    )
    assert len(made) == before, made
    assert _by_key(made, "ble_free_slots").native_value == 0


def test_only_the_count_is_a_measurement() -> None:
    """A state code averaged over a day is a number nothing ever reported."""
    slots = PROFILES.ROWS[("BleDeviceManagement", "NrFreeSlots")][0]
    assert slots.state_class is not None

    for param in ("BleConnState", "State"):
        row = PROFILES.ROWS[("BleDeviceManagement", param)][0]
        assert row.state_class is None, param
        assert row.unit is None and row.scale is None, param


def test_all_three_are_diagnostics_that_are_on() -> None:
    """Diagnostics, so they sit under the fold rather than beside a reading.

    And on: what they are for is being in the recorder *before* the session
    that wedges, and an entity nobody has enabled records nothing. That is
    what separates them from the raw flame value, which is off by default --
    that one restates a reading already shown as a binary sensor, and nothing
    here restates anything.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    for param in ("NrFreeSlots", "BleConnState", "State"):
        coordinator.report("BleDeviceManagement", param, 0, BLE_MGMT)

    for key in KEYS:
        entity = _by_key(made, key)
        assert entity._attr_entity_category is stubs.EntityCategory.DIAGNOSTIC, key
        assert entity._attr_entity_registry_enabled_default, key


def test_no_state_code_is_given_names_we_made_up() -> None:
    """The panel publishes no enum for any of them (measured on the van).

    Labels here are ours rather than the panel's -- see the select rows -- so
    inventing a set for a code nothing explains would put words on a value
    whose meaning is unknown.
    """
    for param in ("NrFreeSlots", "BleConnState", "State"):
        for row in PROFILES.ROWS[("BleDeviceManagement", param)]:
            assert row.labels is None, param
            assert row.platform == "sensor", param


def test_the_free_slots_are_summed_out_of_the_breakdown() -> None:
    """The parameter is a count per device kind, not a count.

    Measured on the van it read ``[{"type": 12, "nrOfSlots": 1}, {"type": 9,
    "nrOfSlots": 2}]``, and nothing published says what the kinds are -- so
    the entity is their sum, and the breakdown stays in the download.
    """
    row = PROFILES.ROWS[("BleDeviceManagement", "NrFreeSlots")][0]

    assert PROFILES.native(row, FREE_SLOTS) == 3
    assert PROFILES.native(row, []) == 0
    # Nothing is invented out of a shape that is not the measured one.
    assert PROFILES.native(row, 2) is None
    assert PROFILES.native(row, [{"type": 12}]) is None


def test_a_structure_no_row_reduces_reads_unknown_rather_than_raising() -> None:
    """The failure this cost, before the row reduced anything.

        ValueError: Sensor sensor.…_free_slots has device class 'None', state
        class 'measurement' … TypeError: float() argument must be a string or
        a real number, not 'list'

    Home Assistant raises that from inside the state write, so it repeats on
    every coordinator update for as long as the integration runs. A reading
    that cannot be shown is missing either way -- this makes it missing
    quietly, and names the row to fix.
    """
    coordinator = _coordinator()
    made = stubs.setup_platform(SENSOR, coordinator)
    # A room temperature is a scalar everywhere it has been measured; the
    # point is that no sensor raises, whatever a panel sends.
    coordinator.report("AirHeating", "Temp", [{"unexpected": 1}], HEATER)

    assert _by_key(made, "current_temp").native_value is None


def _main() -> None:
    stubs.run_tests(globals(), "BLE management entities")


if __name__ == "__main__":
    _main()
