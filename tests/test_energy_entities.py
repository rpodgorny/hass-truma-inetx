#!/usr/bin/env python3
"""Offline checks for the energy sources, the batteries and the raw flame value.

No hardware and no Home Assistant install: HA is stubbed and the real
``truma/state.py``, ``sensor.py``, ``switch.py`` and ``binary_sensor.py`` are
loaded against it, then the entities are constructed and read.

Why this exists:

- **#16.** ``EnergySrc.GasLevel`` was not mapped at all, so a gas/electric
  Combi had no gas reading. It is writable, and a write does go through -- but
  the heater writes it too: measured on that vehicle, switching the electric
  element off moved the gas source on by itself. A switch would fight the
  heater and flap, so gas is reflected and never commanded.
- The same issue exposes the mirror-image bug: the diesel switch was created on
  every vehicle, including gas Combis with no diesel burner and no
  ``EnergySrc.DieselLevel`` at all.
- **#17.** The starter and leisure battery voltages ride on the electrical
  block, in tenths of a volt -- a different scale from ``Eol.Vcc12``, which is
  millivolts.
- **#15.** ``System.FlameStatus`` takes 0, 1 and 2, and nothing published says
  what they mean. The binary sensor must answer on/off, so the raw value needs
  somewhere to be seen -- and the on/off question turned out to be answerable:
  measured on a Combi 6 E against a shore-power meter, 2 is the appliance
  standing by, not a second kind of firing.

What it pins:

1. gas, diesel and both batteries reach the state fields their entities read,
2. gas is a read-only reflection -- no platform writes ``EnergySrc.GasLevel``,
3. each of them is created when, and only when, its parameter is reported,
4. the batteries are scaled by ten, not by a thousand,
5. the raw flame value is exposed unrounded, as a disabled diagnostic,
6. and the flame binary sensor is on while the burner runs and off while it
   merely stands by.

Run: ``python3 tests/test_energy_entities.py``
"""

from __future__ import annotations

import asyncio
import enum
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    """The subset of Home Assistant's description fields the platforms set."""

    key: str
    translation_key: str | None = None
    device_class: object | None = None
    state_class: object | None = None
    native_unit_of_measurement: str | None = None
    suggested_display_precision: int | None = None
    entity_category: object | None = None
    entity_registry_enabled_default: bool = True


class _EntityCategory(enum.StrEnum):
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


def _load():
    """Import the real platforms with Home Assistant stubbed out."""

    class _CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod(
        "homeassistant.const",
        PERCENTAGE="%",
        EntityCategory=_EntityCategory,
        UnitOfElectricPotential=types.SimpleNamespace(VOLT="V"),
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"),
        UnitOfTime=types.SimpleNamespace(SECONDS="s"),
    )
    _mod("homeassistant.helpers", __path__=[])
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity", Entity=object)
    _mod("homeassistant.helpers.entity_platform",
         AddConfigEntryEntitiesCallback=object)
    _mod("homeassistant.helpers.update_coordinator",
         CoordinatorEntity=_CoordinatorEntity)
    _mod("homeassistant.components", __path__=[])
    _mod(
        "homeassistant.components.sensor",
        SensorEntity=object,
        SensorEntityDescription=_EntityDescription,
        SensorDeviceClass=types.SimpleNamespace(
            TEMPERATURE="temperature", VOLTAGE="voltage", DURATION="duration", ENUM="enum"
        ),
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    _mod(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=object,
        BinarySensorDeviceClass=types.SimpleNamespace(
            RUNNING="running", CONNECTIVITY="connectivity"
        ),
    )
    _mod(
        "homeassistant.components.switch",
        SwitchEntity=object,
        SwitchDeviceClass=types.SimpleNamespace(SWITCH="switch"),
    )
    _mod("homeassistant.components.select", SelectEntity=object)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])

    class _Coordinator:
        def __class_getitem__(cls, _item):
            return cls

    _mod("truma_pkg.coordinator", TrumaCoordinator=_Coordinator,
         TrumaConfigEntry=object)

    def _real(name: str, package: str = "truma_pkg", path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    state = _real("state", "truma_pkg.truma", SRC / "truma")
    _real("const")
    _real("entity")
    return (
        state,
        _real("sensor"),
        _real("switch"),
        _real("binary_sensor"),
        _real("select"),
    )


STATE, SENSOR, SWITCH, BINARY, SELECT = _load()

# The electrical block, as measured on the vehicles reported so far. Named
# here only to prove nothing in the source needs to name it.
BOARD = 0x0405
HEATER = 0x0201


class _FakeCoordinator:
    """Enough coordinator to run a platform's setup and read its entities."""

    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self, state=None) -> None:
        self.data = state if state is not None else STATE.TrumaState()
        self._listeners: list = []
        coordinator = self

        class _Entry:
            runtime_data = coordinator

            @staticmethod
            def async_on_unload(_unsub) -> None:
                pass

        self.config_entry = _Entry()
        self.entry = _Entry()
        self.writes: list = []

    def async_add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def report(self, topic: str, param: str, value: int, src: int = BOARD) -> None:
        """Deliver a parameter the way a decoded frame would."""
        self.data.update(topic, param, value, src)
        for cb in list(self._listeners):
            cb()

    async def async_write(self, topic: str, param: str, value: int) -> None:
        self.writes.append((topic, param, value))

    async def async_write_many(self, commands, *, confirm=False, action=None, target=None) -> None:
        assert confirm
        self.writes.extend(commands)


def _setup(platform, coordinator) -> list:
    """Run a platform's real setup, collecting what it creates."""
    made: list = []
    asyncio.run(
        platform.async_setup_entry(None, coordinator.entry, lambda new: made.extend(new))
    )
    return made


def _names(entities) -> list[str]:
    return [type(entity).__name__ for entity in entities]


def _by_key(entities, key: str):
    for entity in entities:
        if entity.entity_description.key == key:
            return entity
    raise AssertionError(f"no sensor with key {key}")


def test_the_new_params_reach_the_fields_their_entities_read() -> None:
    s = STATE.TrumaState()
    s.update("EnergySrc", "GasLevel", 1, HEATER)
    s.update("EnergySrc", "DieselLevel", 0, HEATER)
    s.update("VBat", "Voltage", 137, BOARD)
    s.update("L1Bat", "Voltage", 126, BOARD)

    assert s.gas_level == 1
    assert s.diesel_level == 0
    assert s.starter_battery_voltage == 137
    assert s.leisure_battery_voltage == 126


def test_gas_is_reflected_and_never_commanded() -> None:
    """#16: the heater writes GasLevel itself, so a switch would flap.

    Measured on a gas/electric Combi 6 E: NeedsEnergySrc read 1 throughout and
    switching the electric element off moved the gas source on with nothing
    written from Home Assistant.
    """
    gas = BINARY.TrumaGasSensor(_FakeCoordinator())
    assert not hasattr(gas, "async_turn_on")
    assert not hasattr(gas, "async_turn_off")

    # ...and no platform writes the parameter anywhere.
    for platform in ("switch", "select", "number", "climate", "binary_sensor"):
        text = (SRC / f"{platform}.py").read_text()
        assert "GasLevel" not in text or platform in ("binary_sensor", "select"), (
            f"{platform}.py touches EnergySrc.GasLevel"
        )
    assert '"GasLevel"' not in (SRC / "binary_sensor.py").read_text(), (
        "binary_sensor.py writes the parameter rather than reading it"
    )


def test_the_gas_sensor_appears_only_on_a_heater_that_burns_gas() -> None:
    coordinator = _FakeCoordinator()
    made = _setup(BINARY, coordinator)
    assert _names(made) == [
        "TrumaFlameSensor",
        "TrumaConnectionSensor",
        "TrumaProxySensor",
    ], made

    coordinator.report("EnergySrc", "GasLevel", 1, HEATER)
    assert _names(made)[-1] == "TrumaGasSensor", made
    assert made[-1].is_on is True

    # ...and only once, however many frames follow.
    coordinator.report("EnergySrc", "GasLevel", 0, HEATER)
    assert len(made) == 4, made
    assert made[-1].is_on is False


def test_diesel_is_controlled_only_by_the_energy_source_select() -> None:
    coordinator = _FakeCoordinator()
    made = _setup(SWITCH, coordinator)
    assert made == [], "a gas Combi was given a diesel burner switch"

    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    assert made == []


def test_energy_source_appears_for_diesel_and_expands_when_electric_reports() -> None:
    coordinator = _FakeCoordinator()
    made = _setup(SELECT, coordinator)
    assert _names(made) == ["TrumaWaterModeSelect"]

    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    assert _names(made) == ["TrumaWaterModeSelect", "TrumaEnergySourceSelect", "TrumaElectricLevelSelect"]

    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    assert _names(made) == [
        "TrumaWaterModeSelect",
        "TrumaEnergySourceSelect",
        "TrumaElectricLevelSelect",
    ]


def test_each_single_source_shows_both_disabled_fields_and_rejects_writes():
    for param in ("DieselLevel", "GasLevel", "ElectricLevel"):
        for value in (0, 1):
            coordinator = _FakeCoordinator()
            coordinator.data.connected = True
            made = _setup(SELECT, coordinator)
            coordinator.report("EnergySrc", param, value, HEATER)
            assert _names(made) == ["TrumaWaterModeSelect", "TrumaEnergySourceSelect", "TrumaElectricLevelSelect"]
            for entity, options in ((made[1], ("off", "diesel", "electric", "hybrid")), (made[2], ("off", "900 W", "1800 W"))):
                assert not entity.available, (param, value, type(entity).__name__)
                assert entity.options == []
                for option in options:
                    try:
                        asyncio.run(entity.async_select_option(option))
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError(f"single source accepted {option}")
            assert not coordinator.writes
            coordinator.report("EnergySrc", param, value, HEATER)
            assert len(made) == 3


def test_second_source_enables_existing_entities_by_hardware_not_active_values():
    for first, second in (("DieselLevel", "ElectricLevel"), ("ElectricLevel", "DieselLevel")):
        coordinator = _FakeCoordinator()
        coordinator.data.connected = True
        made = _setup(SELECT, coordinator)
        coordinator.report("EnergySrc", first, 0, HEATER)
        assert len(made) == 3, "both fields must appear for the first source"
        source, electric = made[1:]
        assert not source.available and not electric.available
        coordinator.report("EnergySrc", second, 0, HEATER)
        assert len(made) == 3
        assert source.available
        assert source.options == ["diesel", "electric", "hybrid"]
        assert not electric.available
        try:
            asyncio.run(electric.async_select_option("900 W"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("electric power write accepted while disabled in diesel")
        coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)
        assert electric.available
        assert electric.options == ["900 W", "1800 W"]
        assert made[1:] == [source, electric]


def test_standalone_electric_off_is_filtered_by_panel_metadata():
    coordinator = _FakeCoordinator()
    coordinator.report("EnergySrc", "GasLevel", 0, HEATER)
    coordinator.data.learn_param("EnergySrc", "ElectricLevel", {
        "type": 2, "enum": [
            {"n": "Off", "a": False, "v": 0},
            {"n": "900 W", "a": True, "v": 1},
            {"n": "1800 W", "a": True, "v": 2},
        ],
    })
    coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)
    entity = SELECT.TrumaElectricLevelSelect(coordinator)
    assert entity.options == ["900 W", "1800 W"]
    try:
        asyncio.run(entity.async_select_option("off"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("metadata-excluded off option was accepted")
    assert not coordinator.writes


def test_energy_source_maps_states_and_uses_safe_900_w_transitions() -> None:
    coordinator = _FakeCoordinator()
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    source = SELECT.TrumaEnergySourceSelect(coordinator)

    assert source.options == ["diesel", "electric", "hybrid"]
    assert source.current_option == "diesel"

    asyncio.run(source.async_select_option("electric"))
    assert coordinator.writes == [
        ("EnergySrc", "ElectricLevel", 1),
        ("EnergySrc", "DieselLevel", 0),
    ]

    coordinator.writes.clear()
    asyncio.run(source.async_select_option("hybrid"))
    assert coordinator.writes == [
        ("EnergySrc", "DieselLevel", 1),
        ("EnergySrc", "ElectricLevel", 1),
    ]

    coordinator.writes.clear()
    asyncio.run(source.async_select_option("diesel"))
    assert coordinator.writes == [
        ("EnergySrc", "DieselLevel", 1),
        ("EnergySrc", "ElectricLevel", 0),
    ]


def test_electric_power_is_only_available_for_electric_or_hybrid() -> None:
    coordinator = _FakeCoordinator()
    coordinator.data.connected = True
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    level = SELECT.TrumaElectricLevelSelect(coordinator)

    assert level.available is False
    assert level.current_option is None
    assert level.options == ["900 W", "1800 W"]

    coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)
    assert level.available is True
    assert level.current_option == "900 W"


def test_standalone_electric_heater_retains_off_and_can_start_from_zero():
    coordinator = _FakeCoordinator()
    coordinator.data.connected = True
    coordinator.report("EnergySrc", "GasLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    made = _setup(SELECT, coordinator)
    source = made[1]
    assert not source.available
    assert source.options == []
    try:
        asyncio.run(source.async_select_option("electric"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("gas source selector accepted unsupported write")
    level = SELECT.TrumaElectricLevelSelect(coordinator)
    assert level.available, "gas/electric heater cannot turn on its electric element"
    assert level.options == ["off", "900 W", "1800 W"]
    assert level.current_option == "off"
    asyncio.run(level.async_select_option("900 W"))
    asyncio.run(level.async_select_option("off"))
    assert coordinator.writes == [
        ("EnergySrc", "ElectricLevel", 1), ("EnergySrc", "ElectricLevel", 0)
    ]
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    assert not level.available
    assert level.options == ["900 W", "1800 W"]
    assert level.current_option is None


def test_the_batteries_appear_only_where_something_reports_them() -> None:
    """#17: they come off the electrical block, which most vehicles lack."""
    coordinator = _FakeCoordinator()
    made = _setup(SENSOR, coordinator)
    assert "starter_battery_voltage" not in [
        entity.entity_description.key for entity in made
    ]

    coordinator.report("VBat", "Voltage", 137)
    coordinator.report("L1Bat", "Voltage", 126)
    keys = [entity.entity_description.key for entity in made]
    assert "starter_battery_voltage" in keys and "leisure_battery_voltage" in keys, keys


def test_the_batteries_are_tenths_of_a_volt_not_millivolts() -> None:
    """137 is 13.7 V. Eol.Vcc12 beside them is millivolts; these are not."""
    coordinator = _FakeCoordinator()
    made = _setup(SENSOR, coordinator)
    coordinator.report("VBat", "Voltage", 137)
    coordinator.report("L1Bat", "Voltage", 126)

    assert _by_key(made, "starter_battery_voltage").native_value == 13.7
    assert _by_key(made, "leisure_battery_voltage").native_value == 12.6
    # The supply voltage keeps its own, different scale.
    coordinator.report("Eol", "Vcc12", 13700)
    assert _by_key(made, "voltage").native_value == 13.7


def test_the_raw_flame_value_is_visible_and_unrounded() -> None:
    """#15: the binary sensor has to say on/off; the number says what it saw."""
    coordinator = _FakeCoordinator()
    made = _setup(SENSOR, coordinator)
    raw = _by_key(made, "flame_status")

    assert raw.entity_description.entity_registry_enabled_default is False
    assert raw.entity_description.entity_category is _EntityCategory.DIAGNOSTIC

    assert raw.native_value is None
    coordinator.report("System", "FlameStatus", 2, HEATER)
    assert raw.native_value == 2, "the third state was folded away again"


def test_the_flame_sensor_is_on_only_while_it_is_firing() -> None:
    """#15, measured: 0 off, 1 running, 2 idle -- not 0 off, anything else on.

    On a Combi 6 E the value went 1 -> 2 in the same second shore power fell
    from 1787 W to 105 W. Read as ``> 0``, standing by looked like a flame.
    """
    coordinator = _FakeCoordinator()
    # Created unconditionally, unlike gas -- every heater has a burner state.
    flame = BINARY.TrumaFlameSensor(coordinator)

    assert flame.is_on is None, "nothing reported yet is unknown, not off"
    coordinator.report("System", "FlameStatus", 0, HEATER)
    assert flame.is_on is False
    coordinator.report("System", "FlameStatus", 1, HEATER)
    assert flame.is_on is True
    coordinator.report("System", "FlameStatus", 2, HEATER)
    assert flame.is_on is False, "2 is the appliance standing by, not a flame"


def test_energy_confirmation_log_requires_confirmed_write() -> None:
    coordinator = _FakeCoordinator()
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 0, HEATER)
    entity = SELECT.TrumaEnergySourceSelect(coordinator)
    events = []
    entity.entity_id = "select.test_energy"
    entity.hass = types.SimpleNamespace(
        config=types.SimpleNamespace(language="de"),
        bus=types.SimpleNamespace(async_fire=lambda name, data: events.append((name, data))),
    )
    asyncio.run(entity.async_select_option("hybrid"))
    assert len(events) == 1, "confirmed setting is missing from activity log"
    assert events[0][0] == "logbook_entry"
    assert events[0][1]["entity_id"] == "select.test_energy"
    assert "Hybrid" in events[0][1]["message"]
    events.clear()
    async def fail(*args, **kwargs):
        raise RuntimeError("no panel confirmation")
    coordinator.async_write_many = fail
    try:
        asyncio.run(entity.async_select_option("electric"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("unconfirmed setting must fail")
    assert not events, "failed command was logged as confirmed"


def test_energy_transaction_masks_intermediate_hybrid_and_rejects_changing():
    coordinator = _FakeCoordinator()
    coordinator.report("EnergySrc", "DieselLevel", 1, HEATER)
    coordinator.report("EnergySrc", "ElectricLevel", 1, HEATER)
    coordinator.energy_source_changing = True
    entity = SELECT.TrumaEnergySourceSelect(coordinator)
    assert entity.current_option == "changing"
    assert "changing" in entity.options
    try:
        asyncio.run(entity.async_select_option("changing"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("status-only option accepted as command")
    assert not coordinator.writes
    coordinator.energy_source_changing = False
    assert entity.current_option == "hybrid"
    assert "changing" not in entity.options


def test_operation_sensor_exists_before_panel_data_and_reports_offline_error():
    coordinator = _FakeCoordinator()
    coordinator.operation_state = "error"
    coordinator.operation_attributes = {"action": "sync", "target": None, "error": "No connection"}
    made = _setup(SENSOR, coordinator)
    found = [e for e in made if e.entity_description.key == "operation"]
    assert len(found) == 1
    assert found[0].available
    assert found[0].native_value == "error"
    assert found[0].extra_state_attributes == coordinator.operation_attributes


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("energy entities: all checks OK")


if __name__ == "__main__":
    _main()
