#!/usr/bin/env python3
"""Offline checks for offering what the panel says this vehicle has.

No hardware, no Home Assistant install: HA is stubbed and the real
``truma/state.py``, ``climate.py`` and ``select.py`` are loaded against it. The
entity properties are called unbound, so nothing has to be constructed.

Why this exists. A list of modes written into the source is wrong in both
directions at once. Measured on two vehicles:

- this van's panel enumerates ``RoomClimate.Mode`` as
  ``{0: Off, 3: Heating, 5: Ventilating}`` -- it has no air conditioner, and 1
  and 2 are absent from the enum rather than present-and-unavailable,
- the Combi 6 E in #11 reports 1 = automatic and 2 = cooling on the same
  parameter, verified at the panel.

Offering a fixed ``[0, 3, 5]`` locks that vehicle out of its own air
conditioner; offering everything puts cooling on a van that cannot cool. So the
panel is asked. The same goes for the water-heating steps (#12).

What it pins:

1. the values the panel enumerates are what gets offered, and a panel that
   describes nothing still gets the list this integration always had,
2. a value the panel marks unavailable for this vehicle is not offered,
3. the panel's *names* never reach a user-facing string -- they arrive in the
   panel's display language (``40 / 60 / 70`` here, Eco / Comfort / Hot on the
   Combi 6 E), so only the values cross over,
4. a write is validated against the panel's enum where there is one, so a mode
   this heater really has stops being rejected by our own table,
5. off stays part of water heating, while energy source owns switching the
   electric element off,
6. and the electric select is not created at all until the vehicle reports an
   electric element -- a Combi D has none, and its panel never mentions the
   parameter.

Run: ``python3 tests/test_panel_declared_options.py``
"""

from __future__ import annotations

import enum
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _HVACMode(enum.StrEnum):
    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class _Feature(enum.IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    TURN_OFF = 128
    TURN_ON = 256


def _load():
    """Import the real climate/select platforms with HA and BLE stubbed."""
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod("homeassistant.const", ATTR_TEMPERATURE="temperature",
         UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"),
         CONF_ADDRESS="address", CONF_NAME="name")
    _mod("homeassistant.helpers", __path__=[], issue_registry=types.SimpleNamespace(
        async_create_issue=lambda *a, **kw: None,
        async_delete_issue=lambda *a, **kw: None,
        IssueSeverity=types.SimpleNamespace(WARNING="warning"),
    ))
    _mod("homeassistant.helpers.storage", Store=object)
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity", Entity=object)
    _mod("homeassistant.helpers.entity_platform",
         AddConfigEntryEntitiesCallback=object)
    _mod("bleak_retry_connector", BleakClientWithServiceCache=object,
         establish_connection=None)

    class _Coordinator:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator=None) -> None:
            self.coordinator = coordinator

        @property
        def available(self):
            return True

    _mod("homeassistant.helpers.update_coordinator",
         DataUpdateCoordinator=_Coordinator, CoordinatorEntity=_Coordinator)
    _mod("homeassistant.components", __path__=[])
    _mod("homeassistant.components.climate", ClimateEntity=object,
         ClimateEntityFeature=_Feature, HVACMode=_HVACMode, FAN_OFF="off")
    _mod("homeassistant.components.select", SelectEntity=object)

    _mod("truma_pkg", __path__=[str(SRC)])
    _mod("truma_pkg.truma", __path__=[str(SRC / "truma")])
    _mod("truma_pkg.ble", TrumaBleClient=object, device_from_bluez=None)
    _mod("truma_pkg.bt", async_panel_advertising=lambda *a: False,
         async_remote_scanner_source=lambda *a: None,
         async_resolve_proxy_device=None, async_wait_until_heard=None)
    _mod("truma_pkg.proxy", TrumaProxyTracker=object)

    def _real(name: str, package: str = "truma_pkg", path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _real("const", "truma_pkg.truma", SRC / "truma")
    _real("protocol", "truma_pkg.truma", SRC / "truma")
    state = _real("state", "truma_pkg.truma", SRC / "truma")
    _real("const")
    _real("coordinator")
    _real("entity")
    return state, _real("climate"), _real("select")


STATE, CLIMATE, SELECT = _load()


class _Holder:
    """Stands in for an entity: the properties only read ``self.data``."""

    def __init__(self, state) -> None:
        self.data = state


def _described(state, topic: str, param: str, names: dict, **extra) -> None:
    """Feed a panel description in, the shape the panel actually sends it."""
    state.learn_param(topic, param, {
        "pn": param,
        "type": 2,
        "avail": 1,
        "enum": [{"n": name, "a": True, "v": value} for value, name in names.items()],
        **extra,
    })


# The two vehicles this is about, as their panels described themselves.
THIS_VAN = {0: "Off", 3: "Heating", 5: "Ventilating"}
COMBI_6E = {0: "Off", 1: "Automatic", 2: "Cooling", 3: "Heating", 5: "Ventilating"}


def _hvac_modes(state) -> list:
    return CLIMATE.TrumaClimate.hvac_modes.fget(_Holder(state))


def test_a_van_without_cooling_is_not_offered_cooling() -> None:
    """Measured: this panel's enum has no 1 and no 2 in it at all."""
    state = STATE.TrumaState()
    _described(state, "RoomClimate", "Mode", THIS_VAN)

    assert _hvac_modes(state) == ["off", "heat", "fan_only"]


def test_a_vehicle_that_has_cooling_is_offered_it() -> None:
    """The whole of #11: the modes exist, the hardcoded list hid them."""
    state = STATE.TrumaState()
    _described(state, "RoomClimate", "Mode", COMBI_6E)

    assert _hvac_modes(state) == ["off", "auto", "cool", "heat", "fan_only"]


def test_a_silent_panel_still_gets_the_modes_this_always_offered() -> None:
    """Nothing described means fall back, not an empty climate card."""
    assert _hvac_modes(STATE.TrumaState()) == ["off", "heat", "fan_only"]


def test_a_value_we_have_no_name_for_is_left_out_and_off_stays() -> None:
    """An unknown mode must not become a mode the frontend cannot render."""
    state = STATE.TrumaState()
    _described(state, "RoomClimate", "Mode", {3: "Heating", 9: "Whatever"})

    modes = _hvac_modes(state)
    assert modes == ["off", "heat"], modes


def test_the_water_steps_come_from_the_panel_but_the_words_do_not() -> None:
    """#12: this panel says 40/60/70, a Combi 6 E says Eco/Comfort/Hot.

    Either way the select shows our own labels, which carry both halves -- the
    panel's own names are in the panel's language, and a select option is
    user-facing text automations match on.
    """
    state = STATE.TrumaState()
    _described(state, "WaterHeating", "Mode", {0: "Eco", 1: "Comfort", 2: "Hot"})

    options = SELECT.TrumaWaterModeSelect.options.fget(_Holder(state))
    assert options == ["off", "Eco (40 °C)", "Comfort (60 °C)", "Hot (70 °C)"], options


def test_a_step_the_vehicle_does_not_have_is_not_offered() -> None:
    """A panel can describe a value and mark it unavailable here."""
    state = STATE.TrumaState()
    state.learn_param("EnergySrc", "ElectricLevel", {
        "type": 2,
        "enum": [
            {"n": "Off", "a": True, "v": 0},
            {"n": "900 W", "a": True, "v": 1},
            {"n": "1800 W", "a": False, "v": 2},
        ],
    })
    state.update("EnergySrc", "GasLevel", 0)
    state.update("EnergySrc", "ElectricLevel", 1)
    entity = SELECT.TrumaElectricLevelSelect(_FakeCoordinator(state))
    assert entity.options == ["off", "900 W"], entity.options
    state.update("EnergySrc", "DieselLevel", 1)
    state.update("EnergySrc", "ElectricLevel", 0)
    assert entity.options == ["900 W"]


def test_a_silent_panel_uses_the_safe_fallback_steps() -> None:
    state = STATE.TrumaState()
    assert SELECT.TrumaWaterModeSelect.options.fget(_Holder(state)) == [
        "off", "Eco (40 °C)", "Comfort (60 °C)", "Hot (70 °C)",
    ]
    state.update("EnergySrc", "GasLevel", 0)
    state.update("EnergySrc", "ElectricLevel", 0)
    assert SELECT.TrumaElectricLevelSelect(_FakeCoordinator(state)).options == [
        "off", "900 W", "1800 W",
    ]


def test_the_panel_outranks_our_table_when_it_has_spoken() -> None:
    """PARAM_VALIDATION says [0, 3, 5]; this vehicle says otherwise."""
    state = STATE.TrumaState()
    assert state.validate_write("RoomClimate", "Mode", 2)[0] is False

    _described(state, "RoomClimate", "Mode", COMBI_6E)
    ok, msg = state.validate_write("RoomClimate", "Mode", 2)
    assert ok, msg
    # ...and it constrains as well as permits: 4 is in the protocol, not on
    # this vehicle's panel.
    assert state.validate_write("RoomClimate", "Mode", 4)[0] is False


def test_our_table_still_applies_where_the_panel_said_nothing() -> None:
    state = STATE.TrumaState()
    assert state.validate_write("Switches", "FreshWaterPump", 1)[0] is True
    assert state.validate_write("Switches", "FreshWaterPump", 2)[0] is False
    # Ranges keep working too -- those are never enums.
    assert state.validate_write("RoomClimate", "TgtTemp", 200)[0] is True
    assert state.validate_write("RoomClimate", "TgtTemp", 900)[0] is False


def test_allowed_values_reports_values_never_names() -> None:
    """The names are evidence for us, not strings for anybody else."""
    state = STATE.TrumaState()
    _described(state, "WaterHeating", "Mode", {0: "Eco", 1: "Comfort", 2: "Hot"})

    assert state.allowed_values("WaterHeating", "Mode") == [0, 1, 2]
    assert state.allowed_values("WaterHeating", "Nothing") is None


class _FakeCoordinator:
    """Enough coordinator for the select platform's setup to run."""

    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self, state) -> None:
        self.data = state
        self._listeners: list = []
        coordinator = self

        class _Entry:
            runtime_data = coordinator

            @staticmethod
            def async_on_unload(_unsub) -> None:
                pass

        self.config_entry = _Entry()
        self.entry = _Entry()

    def async_add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def report(self, topic: str, param: str, value: int) -> None:
        """Deliver a parameter the way a decoded frame would."""
        self.data.update(topic, param, value)
        for cb in list(self._listeners):
            cb()


def _setup_selects(coordinator) -> list:
    """Run the real platform setup, collecting what it creates."""
    made: list = []
    import asyncio

    asyncio.run(
        SELECT.async_setup_entry(None, coordinator.entry, lambda new: made.extend(new))
    )
    return made


def test_energy_fields_appear_once_on_any_source_and_single_source_is_disabled() -> None:
    """Both fields remain visible but disabled for single-source hardware."""
    coordinator = _FakeCoordinator(STATE.TrumaState())
    made = _setup_selects(coordinator)

    assert [type(entity).__name__ for entity in made] == ["TrumaWaterModeSelect"], made

    # One reported source creates both fields, without enabling either.
    coordinator.report("EnergySrc", "ElectricLevel", 0)
    assert [type(entity).__name__ for entity in made] == [
        "TrumaWaterModeSelect",
        "TrumaEnergySourceSelect",
        "TrumaElectricLevelSelect",
    ], made
    assert not made[1].available and not made[2].available
    assert made[1].options == [] and made[2].options == []

    # ...and only once, however many frames follow.
    coordinator.report("EnergySrc", "ElectricLevel", 1)
    assert len(made) == 3, made


def test_water_heating_is_not_gated_on_anything() -> None:
    """Every vehicle this runs on has water heating; it is the heater itself."""
    made = _setup_selects(_FakeCoordinator(STATE.TrumaState()))
    assert len(made) == 1


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("panel-declared options: all checks OK")


if __name__ == "__main__":
    _main()
