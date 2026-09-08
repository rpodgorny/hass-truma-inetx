#!/usr/bin/env python3
"""Offline checks for the two water-priority modes and their duration.

No hardware and no Home Assistant install: HA is stubbed and the real
``truma/state.py``, ``switch.py`` and ``sensor.py`` are loaded against it, then
the entities are constructed and read.

Why this exists: the panel offers a mode that puts the burner's whole output
into the boiler, and the reverse-engineered schema in
`daaaaan/truma-inetx-ble` shows two parameters that could be it --
``WaterHeating.BoostMode`` and ``WaterHeating.FasterHeatingMode``, both
documented as 0/1 and nothing else, with ``FasterHeatingModeTime`` (seconds)
beside the second. Neither has been seen in a dump from a vehicle yet.

That is the whole reason for these checks. Both are offered so the vehicle can
say which one it has, and neither may be created before its own parameter has
been reported -- otherwise a heater with only one of them, or neither, is given
a switch that writes into nothing.

What it pins:

1. all three parameters reach the state fields the entities read,
2. each switch appears when, and only when, its own parameter is reported --
   one arriving must not conjure the other,
3. the writes carry the parameter's own name and 0/1, and are addressed to the
   heater like every other WaterHeating write,
4. the duration is reported raw, in seconds, as a disabled-free diagnostic
   with no state class -- nothing yet says whether it counts down.

Run: ``python3 tests/test_water_priority.py``
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

PANEL = 0x0101
HEATER = 0x0201


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

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
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
            TEMPERATURE="temperature", VOLTAGE="voltage", DURATION="duration"
        ),
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    _mod(
        "homeassistant.components.switch",
        SwitchEntity=object,
        SwitchDeviceClass=types.SimpleNamespace(SWITCH="switch"),
    )

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
    return state, _real("switch"), _real("sensor")


STATE, SWITCH, SENSOR = _load()


class _FakeCoordinator:
    """Enough coordinator to run a platform's setup and read its entities."""

    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self) -> None:
        self.data = STATE.TrumaState()
        self._listeners: list = []
        coordinator = self

        class _Entry:
            runtime_data = coordinator

            @staticmethod
            def async_on_unload(_unsub) -> None:
                pass

        self.entry = _Entry()
        self.config_entry = _Entry()
        self.writes: list = []

    def async_add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def report(self, topic: str, param: str, value: int, src: int = HEATER) -> None:
        """Deliver a parameter the way a decoded frame would."""
        self.data.update(topic, param, value, src)
        for cb in list(self._listeners):
            cb()

    async def async_write(self, topic: str, param: str, value: int) -> None:
        self.writes.append((topic, param, value))


def _setup(platform, coordinator) -> list:
    """Run a platform's real setup, collecting what it creates."""
    made: list = []
    asyncio.run(
        platform.async_setup_entry(None, coordinator.entry, lambda new: made.extend(new))
    )
    return made


def _names(entities) -> list[str]:
    return [type(entity).__name__ for entity in entities]


def test_the_three_parameters_reach_the_fields_their_entities_read() -> None:
    s = STATE.TrumaState()
    s.update("WaterHeating", "BoostMode", 1, HEATER)
    s.update("WaterHeating", "FasterHeatingMode", 0, HEATER)
    s.update("WaterHeating", "FasterHeatingModeTime", 1800, HEATER)

    assert s.water_boost == 1
    assert s.water_faster_heating == 0
    assert s.water_faster_heating_time == 1800


def test_neither_switch_exists_until_its_own_parameter_arrives() -> None:
    """Both are unmeasured, so a heater that never mentions one gets nothing."""
    coordinator = _FakeCoordinator()
    made = _setup(SWITCH, coordinator)
    assert made == [], "a control was created for an unmeasured parameter"

    coordinator.report("WaterHeating", "BoostMode", 0)
    assert _names(made) == ["TrumaWaterBoostSwitch"], made
    assert made[0].is_on is False

    # One arriving must not conjure the other: they may well not both exist.
    coordinator.report("WaterHeating", "FasterHeatingModeTime", 1800)
    assert _names(made) == ["TrumaWaterBoostSwitch"], made

    coordinator.report("WaterHeating", "FasterHeatingMode", 1)
    assert _names(made) == [
        "TrumaWaterBoostSwitch",
        "TrumaFasterWaterHeatingSwitch",
    ], made
    assert made[1].is_on is True

    # ...and only once, however many frames follow.
    coordinator.report("WaterHeating", "BoostMode", 1)
    assert len(made) == 2, made
    assert made[0].is_on is True


def test_each_switch_writes_its_own_parameter_to_the_heater() -> None:
    coordinator = _FakeCoordinator()
    made = _setup(SWITCH, coordinator)
    coordinator.report("WaterHeating", "BoostMode", 0)
    coordinator.report("WaterHeating", "FasterHeatingMode", 0)
    boost, faster = made

    asyncio.run(boost.async_turn_on())
    asyncio.run(boost.async_turn_off())
    asyncio.run(faster.async_turn_on())
    asyncio.run(faster.async_turn_off())

    assert coordinator.writes == [
        ("WaterHeating", "BoostMode", 1),
        ("WaterHeating", "BoostMode", 0),
        ("WaterHeating", "FasterHeatingMode", 1),
        ("WaterHeating", "FasterHeatingMode", 0),
    ], coordinator.writes

    # WaterHeating is one of the topics whose owner really is fixed, so these
    # go to the heater like the mode and the target do -- not to whoever last
    # relayed the topic.
    assert coordinator.data.get_command_dest("WaterHeating") == HEATER
    for param in ("BoostMode", "FasterHeatingMode"):
        ok, _ = STATE.TrumaState.validate_command("WaterHeating", param, 1)
        assert ok
        ok, msg = STATE.TrumaState.validate_command("WaterHeating", param, 2)
        assert not ok and "2" in msg, f"{param} accepted a value outside 0/1"


def test_the_duration_is_reported_raw_in_seconds() -> None:
    """Nothing says whether it counts down, so it is shown, not interpreted."""
    coordinator = _FakeCoordinator()
    made = _setup(SENSOR, coordinator)
    keys = [entity.entity_description.key for entity in made]
    assert "faster_water_heating_time" not in keys, keys

    coordinator.report("WaterHeating", "FasterHeatingModeTime", 1800)
    timer = next(
        entity
        for entity in made
        if entity.entity_description.key == "faster_water_heating_time"
    )
    assert timer.native_value == 1800, "the duration was scaled on the way out"
    assert timer.entity_description.native_unit_of_measurement == "s"
    assert timer.entity_description.entity_category is _EntityCategory.DIAGNOSTIC
    assert timer.entity_description.state_class is None, (
        "a statistic of a value that may be a countdown means nothing"
    )


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("water priority: all checks OK")


if __name__ == "__main__":
    _main()
