#!/usr/bin/env python3
"""Offline checks for the water entities and how their writes are addressed.

``truma/state.py`` needs no stubbing at all -- that is the point of keeping it
free of Home Assistant. ``entity.py`` is loaded for real against a stub
coordinator, because the "create it once the hardware reports" helper is the
part with actual logic in it.

Why this exists: the fresh-water pump and the tank levels belong to a device
that is not the heater and not the panel -- an electrical block, at 0x0405 on
the two vehicles reported so far (issues #4, #7 and #8). Its address differs
per vehicle and is renumbered when it is re-paired, so a write cannot be sent
to an address named in the source. And most vehicles have no water hardware at
all, so the entities cannot simply be created for everyone.

What it pins:

1. the three water parameters reach the state fields the entities read,
2. a topic's source address is learned, and the message broker is not mistaken
   for one,
3. the measured destination table still wins for every topic it names, so this
   changes nothing for the heater and the panel,
4. a water write goes to whoever reported the topic, falling back to the panel
   while nothing has,
5. the pump only accepts 0 and 1,
6. an entity is created when -- and only when -- its parameter is reported,
   and only once,
7. every translation_key in the platforms has a name in strings.json, and the
   water sensors carry an icon, having no device class to draw one from.

Run: ``python3 tests/test_water_entities.py``
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "truma_inetx"

PANEL = 0x0101
HEATER = 0x0201
BROKER = 0x0000
# The electrical block, as measured on a Weinsberg and on a second vehicle.
# Named here only to prove nothing in the source needs to name it.
BOARD = 0x0405


def _load_state():
    """Import truma/state.py alone -- it imports nothing but the stdlib."""
    spec = importlib.util.spec_from_file_location(
        "truma_state_only", SRC / "truma" / "state.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load_entity():
    """Import the real entity.py against stubs for everything it leans on."""

    class _CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", callback=lambda f: f)
    _mod("homeassistant.helpers", __path__=[])
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity", Entity=object)
    _mod(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
    )
    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])

    class _Coordinator:
        def __class_getitem__(cls, _item):
            return cls

    _mod("truma_pkg.coordinator", TrumaCoordinator=_Coordinator)
    _mod("truma_pkg.truma.state", TrumaState=object)

    def _real(name: str, path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"truma_pkg.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_pkg.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _real("const")
    return _real("entity")


STATE = _load_state()
ENTITY = _load_entity()


class _FakeCoordinator:
    """Carries only what async_add_when_reported touches."""

    def __init__(self, raw: dict | None = None) -> None:
        self.data = types.SimpleNamespace(raw_params=dict(raw or {}))
        self._listeners: list = []
        self.unsubs = 0
        coordinator = self

        class _Entry:
            @staticmethod
            def async_on_unload(unsub):
                coordinator.on_unload = unsub

        self.config_entry = _Entry()
        self.on_unload = None

    def async_add_listener(self, cb):
        self._listeners.append(cb)

        def _unsub():
            self.unsubs += 1
            self._listeners.remove(cb)

        return _unsub

    def report(self, key: str, value=1) -> None:
        """Deliver a parameter the way a decoded frame would."""
        self.data.raw_params[key] = value
        for cb in list(self._listeners):
            cb()


def test_water_params_reach_the_entity_fields() -> None:
    s = STATE.TrumaState()
    s.update("FreshWater", "Level", 25, BOARD)
    s.update("GreyWater", "Level", 0, BOARD)
    s.update("Switches", "FreshWaterPump", 1, BOARD)

    assert s.fresh_water_level == 25
    assert s.grey_water_level == 0
    assert s.water_pump == 1
    # Levels are a percentage in quarter steps; nothing is scaled on the way
    # in, so a sensor showing 25 must mean the panel said 25.
    assert s.raw_params["FreshWater.Level"] == 25


def test_the_topic_source_is_learned_but_the_broker_is_not() -> None:
    s = STATE.TrumaState()
    s.update("Switches", "FreshWaterPump", 0, BOARD)
    assert s.topic_source["Switches"] == BOARD

    # 0x0000 is the message broker, not a device. Addressing a write there
    # would send it nowhere, so it must never be learned.
    s.update("GreyWater", "Level", 50, BROKER)
    assert "GreyWater" not in s.topic_source
    # ...and a value with no source at all must not invent one.
    s.update("FreshWater", "Level", 75, None)
    assert "FreshWater" not in s.topic_source


def test_the_measured_table_still_wins() -> None:
    """Nothing about the heater or the panel may change."""
    s = STATE.TrumaState()
    # Even when a topic arrives from somewhere unexpected, the fixed entry is
    # what a write follows: some topics are relayed on our behalf, so the
    # sender is not necessarily the right recipient.
    s.update("RoomClimate", "Mode", 0, 0x0999)
    s.update("AirHeating", "Temp", 228, 0x0999)

    assert s.get_command_dest("RoomClimate") == PANEL
    assert s.get_command_dest("AirHeating") == HEATER
    for topic in ("WaterHeating", "AirCirculation", "EnergySrc", "AirCooling"):
        assert s.get_command_dest(topic) == STATE.COMMAND_DEST[topic]


def test_a_water_write_goes_to_whoever_owns_the_topic() -> None:
    s = STATE.TrumaState()
    # Before anything has reported, the old behaviour stands: try the panel.
    assert s.get_command_dest("Switches") == PANEL

    s.update("Switches", "FreshWaterPump", 1, BOARD)
    assert s.get_command_dest("Switches") == BOARD, (
        "the pump write is still addressed to the panel"
    )

    # No address for the water hardware may be written into the source: it is
    # renumbered when the device is re-paired.
    for name in ("state.py", "switch.py", "sensor.py"):
        path = SRC / ("truma/state.py" if name == "state.py" else name)
        assert "0x0405" not in path.read_text(), f"{name} hardcodes the block"


def test_the_pump_only_takes_zero_and_one() -> None:
    ok, _ = STATE.TrumaState.validate_command("Switches", "FreshWaterPump", 1)
    assert ok
    ok, msg = STATE.TrumaState.validate_command("Switches", "FreshWaterPump", 2)
    assert not ok and "2" in msg


def test_an_entity_appears_only_once_its_hardware_reports() -> None:
    made: list[str] = []
    coordinator = _FakeCoordinator()
    ENTITY.async_add_when_reported(
        coordinator,
        lambda new: made.extend(new),
        {
            "FreshWater.Level": lambda: "fresh",
            "Switches.FreshWaterPump": lambda: "pump",
        },
    )
    assert made == [], "created an entity for hardware that never reported"

    coordinator.report("FreshWater.Level", 25)
    assert made == ["fresh"], "the tank reported and got no entity"

    # An unrelated parameter must not conjure the rest.
    coordinator.report("AirHeating.Temp", 228)
    assert made == ["fresh"]

    # Repeated reports of the same parameter must not duplicate the entity.
    coordinator.report("FreshWater.Level", 50)
    assert made == ["fresh"]

    coordinator.report("Switches.FreshWaterPump", 1)
    assert made == ["fresh", "pump"]
    # Everything is accounted for, so nothing should still be listening.
    assert coordinator.unsubs == 0 or not coordinator._listeners


def test_data_already_in_hand_needs_no_listener() -> None:
    """A config-entry reload re-runs setup against a live coordinator."""
    made: list[str] = []
    coordinator = _FakeCoordinator({"GreyWater.Level": 0})
    ENTITY.async_add_when_reported(
        coordinator, lambda new: made.extend(new), {"GreyWater.Level": lambda: "grey"}
    )
    assert made == ["grey"], "an already-reported tank got no entity on reload"
    assert not coordinator._listeners, "listener registered with nothing left to wait for"


def _translation_keys(platform: str) -> set[str]:
    """Translation keys a platform file declares, read as source.

    Importing the platform would drag in Home Assistant, and the point is to
    check the table rather than to run it.
    """
    text = (SRC / f"{platform}.py").read_text()
    return set(re.findall(r'translation_key="([a-z_0-9]+)"', text))


def test_every_new_entity_is_named_and_iconed() -> None:
    strings = json.loads((SRC / "strings.json").read_text())["entity"]
    icons = json.loads((SRC / "icons.json").read_text())["entity"]

    for platform in ("sensor", "switch"):
        for key in _translation_keys(platform):
            assert key in strings.get(platform, {}), (
                f"{platform}.{key} has no name in strings.json"
            )

    # These three are the new ones, and the two sensors have no device class
    # to take an icon from, so one has to be given.
    assert "water_pump" in strings["switch"]
    for key in ("fresh_water_level", "grey_water_level"):
        assert key in strings["sensor"], f"sensor.{key} is unnamed"
        assert key in icons["sensor"], f"sensor.{key} has neither icon nor device class"

    en = (SRC / "translations" / "en.json").read_text()
    assert en == (SRC / "strings.json").read_text(), (
        "strings.json and translations/en.json have drifted again"
    )


def test_optional_sensors_all_declare_what_proves_them() -> None:
    """A description with no parameter behind it would never be created."""
    text = (SRC / "sensor.py").read_text()
    block = text[text.index("OPTIONAL_SENSORS"): text.index("async def async_setup_entry")]
    keys = set(re.findall(r'key="([a-z_0-9]+)"', block))
    mapped = set(re.findall(r'"([a-z_0-9]+)": "[A-Za-z]+\.[A-Za-z]+"', block))
    assert keys == mapped, f"OPTIONAL_SENSOR_PARAM does not cover {keys ^ mapped}"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("water entities: all checks OK")


if __name__ == "__main__":
    _main()
