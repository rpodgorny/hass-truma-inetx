"""Home Assistant stand-ins, so the integration's own code can run offline.

Not a test. Every test here runs with plain ``python3`` and no Home Assistant
install, which means the modules under test have to be imported against
something. That something used to be copied into each test file, and after the
bus rewrite the copy was sixty lines of stubs per test, eight times over, with
the platforms drifting apart as each one grew what it happened to need.

The stubs are deliberately thin: enough shape for an import to succeed and for
the integration's *own* logic to run, and nothing that pretends to be Home
Assistant's behaviour. Anything that actually matters -- which entity is
created, what it reads, where a write goes -- is the integration's own and is
asserted against the real code.

``truma_pkg`` is a fake package name for ``custom_components/truma_inetx``, so
that the package ``__init__`` (which imports Home Assistant for real) never
runs while single modules are loaded out of it.
"""

from __future__ import annotations

import enum
import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "truma_inetx"


def mod(name: str, **attrs) -> ModuleType:
    """Register a module under ``name`` carrying ``attrs``."""
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class Platform(enum.StrEnum):
    """The platforms this integration forwards to."""

    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    CLIMATE = "climate"
    NUMBER = "number"
    SELECT = "select"
    SENSOR = "sensor"
    SWITCH = "switch"


class EntityCategory(enum.StrEnum):
    """Home Assistant's two entity categories."""

    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


class HVACMode(enum.StrEnum):
    """The climate modes this integration maps onto."""

    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    DRY = "dry"
    FAN_ONLY = "fan_only"


class ClimateEntityFeature(enum.IntFlag):
    """Enough of the feature flags to check which ones a mode offers."""

    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    TURN_OFF = 128
    TURN_ON = 256


class CoordinatorEntity:
    """The one base class with behaviour worth standing in for."""

    available = True

    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator


class _Coordinator:
    """DataUpdateCoordinator, which is only ever subscripted and subclassed."""

    def __class_getitem__(cls, _item):
        return cls


def _redact(data, keys):
    """Replace every value under a redacted key name, at any depth."""
    if isinstance(data, dict):
        return {
            k: "**REDACTED**" if k in keys else _redact(v, keys)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact(v, keys) for v in data]
    return data


def install_homeassistant() -> None:
    """Put the Home Assistant modules the integration imports on sys.path."""
    mod("homeassistant", __path__=[])
    mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f,
        Event=object)
    mod("homeassistant.config_entries", ConfigEntry=dict)
    mod(
        "homeassistant.const",
        ATTR_TEMPERATURE="temperature",
        CONF_ADDRESS="address",
        CONF_NAME="name",
        PERCENTAGE="%",
        EntityCategory=EntityCategory,
        Platform=Platform,
        UnitOfElectricPotential=SimpleNamespace(VOLT="V"),
        UnitOfMass=SimpleNamespace(KILOGRAMS="kg"),
        UnitOfTemperature=SimpleNamespace(CELSIUS="°C"),
        UnitOfTime=SimpleNamespace(SECONDS="s"),
    )
    mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    mod("homeassistant.loader", async_get_integration=None)

    mod("homeassistant.helpers", __path__=[], issue_registry=SimpleNamespace(
        async_create_issue=lambda *a, **kw: None,
        async_delete_issue=lambda *a, **kw: None,
        IssueSeverity=SimpleNamespace(WARNING="warning"),
    ))
    # DeviceInfo is a TypedDict in Home Assistant, so at runtime this is the
    # same thing. Declared rather than aliased to plain ``dict`` because the
    # coordinator asks it which keys this Home Assistant takes -- ``dict``
    # has no ``__annotations__`` at all, and answering that question wrongly
    # is how a device ends up hung off nothing.
    class DeviceInfo(TypedDict, total=False):
        identifiers: set
        name: str
        manufacturer: str
        model: str
        serial_number: str
        via_device: tuple
        via_device_id: str

    mod("homeassistant.helpers.device_registry", DeviceInfo=DeviceInfo,
        async_get=lambda _hass: None)
    mod("homeassistant.helpers.entity", Entity=object)
    mod("homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=object)
    mod("homeassistant.helpers.storage", Store=object)
    mod("homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity, DataUpdateCoordinator=_Coordinator)

    mod("homeassistant.components", __path__=[])
    mod("homeassistant.components.diagnostics",
        # Redacting for real, by key name and all the way down, the way the
        # real one does: a download that still carries the panel's address is
        # the kind of bug a test has to be able to see.
        async_redact_data=_redact)
    mod(
        "homeassistant.components.sensor",
        SensorEntity=object,
        SensorDeviceClass=SimpleNamespace(
            TEMPERATURE="temperature",
            VOLTAGE="voltage",
            DURATION="duration",
            TIMESTAMP="timestamp",
            ENUM="enum",
            WEIGHT="weight",
            BATTERY="battery",
        ),
        SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"),
    )
    mod(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=object,
        BinarySensorDeviceClass=SimpleNamespace(
            RUNNING="running",
            CONNECTIVITY="connectivity",
            PLUG="plug",
            PROBLEM="problem",
        ),
    )
    mod("homeassistant.components.switch", SwitchEntity=object,
        SwitchDeviceClass=SimpleNamespace(SWITCH="switch"))
    mod("homeassistant.components.button", ButtonEntity=object)
    mod("homeassistant.components.select", SelectEntity=object)
    mod("homeassistant.components.number", NumberEntity=object,
        NumberMode=SimpleNamespace(SLIDER="slider"))
    mod(
        "homeassistant.components.climate",
        FAN_OFF="off",
        ClimateEntity=object,
        ClimateEntityFeature=ClimateEntityFeature,
        HVACMode=HVACMode,
    )

    mod("bleak_retry_connector", BleakClientWithServiceCache=object,
        establish_connection=None)

    mod("truma_pkg", __path__=[str(SRC)])
    mod("truma_pkg.truma", __path__=[str(SRC / "truma")])


def load(name: str, package: str = "truma_pkg", path: Path = SRC) -> ModuleType:
    """Import one real module out of the integration, by file."""
    spec = importlib.util.spec_from_file_location(
        f"{package}.{name}", path / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def load_truma(name: str) -> ModuleType:
    """Import one module out of the vendored protocol subpackage."""
    return load(name, "truma_pkg.truma", SRC / "truma")


async def _no_link_to_close(_client, _label) -> None:
    """Stand-in for ble.close_link: closing a stub link is a no-op."""


def stub_transport() -> None:
    """Stub the BLE transport modules, for tests that are not about it."""
    mod("truma_pkg.ble", TrumaBleClient=object, device_from_bluez=None,
        close_link=_no_link_to_close)
    mod("truma_pkg.bt", async_panel_advertising=lambda *a: False,
        async_resolve_device=None, async_wait_until_heard=None,
        ADDR_IDENTITY="identity", ADDR_RPA="rpa",
        address_kind=lambda _name, _address: "rpa")


class FakeCoordinator:
    """Enough coordinator to run a platform's real setup and drive entities.

    ``device_info`` is deliberately the simplest thing that identifies a
    device, so that a test can say which bus address an entity landed on
    without this standing in for the real naming -- that is the real
    coordinator's, and tests/test_device_params.py drives the real one.
    """

    unique_id = "Truma iNetX-FFB4D1"

    def __init__(self, bus) -> None:
        self.data = bus
        self._listeners: list = []
        self.writes: list[tuple[int, str, str, int]] = []
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

    def device_info(self, addr: int) -> dict:
        return {"identifiers": {("truma_inetx", f"{self.unique_id}_{addr:04X}")}}

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def report(self, topic: str, param: str, value, src: int) -> None:
        """Deliver a value the way a decoded frame would."""
        self.data.update(topic, param, value, src)
        self._notify()

    def describe(self, topic: str, param: str, src: int, **entry) -> None:
        """Deliver a device's own description of a parameter."""
        self.data.learn_param(topic, param, entry, src)
        if entry.get("v") is not None:
            self.data.update(topic, param, entry["v"], src)
        self._notify()

    async def async_write(self, addr: int, topic: str, param: str, value: int):
        """Validate the way the real coordinator does, then record.

        The validation is not decoration. A stub that records every write
        unconditionally passes whatever an entity offers, so the whole class of
        bug where an entity offers a value the validator then refuses is
        invisible to every test in this suite -- which is exactly how #23
        shipped: the climate entity offered cooling and the write was rejected
        by our own table.
        """
        ok, msg = self.data.validate_write(addr, topic, param, value)
        if not ok:
            raise RuntimeError(f"Invalid Truma command: {msg}")
        self.writes.append((addr, topic, param, value))


def setup_platform(platform, coordinator) -> list:
    """Run a platform's real setup, collecting what it creates."""
    import asyncio

    made: list = []
    asyncio.run(
        platform.async_setup_entry(
            None, coordinator.entry, lambda new: made.extend(new)
        )
    )
    return made


def run_tests(globals_: dict, banner: str) -> None:
    """Run every ``test_*`` in a module, in name order."""
    for name, fn in sorted(globals_.items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print(f"{banner}: all checks OK")
