#!/usr/bin/env python3
"""Offline checks for distinct panel-link and Bluetooth-proxy status."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _RemoteScanner:
    def __init__(self, source: str) -> None:
        self.source = source


class _LocalScanner:
    def __init__(self, source: str) -> None:
        self.source = source


class _ScannerDevice:
    def __init__(self, scanner) -> None:
        self.scanner = scanner


SCANNERS: dict[str, list[_ScannerDevice]] = {}


class _Manager:
    def __init__(self) -> None:
        self.sources: dict[str, object] = {}
        self.callback = None

    def async_scanner_by_source(self, source: str):
        return self.sources.get(source)

    def async_register_scanner_registration_callback(self, callback, source):
        assert source is None
        self.callback = callback
        return lambda: None


MANAGER = _Manager()


def _load():
    class _CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

    class _BinarySensorEntity:
        pass

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.helpers", __path__=[])
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity", Entity=object)
    _mod("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
    _mod("homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity)
    _mod("homeassistant.components", __path__=[])
    _mod(
        "homeassistant.components.binary_sensor",
        BinarySensorDeviceClass=types.SimpleNamespace(
            CONNECTIVITY="connectivity", RUNNING="running"
        ),
        BinarySensorEntity=_BinarySensorEntity,
    )
    _mod(
        "homeassistant.components.bluetooth",
        async_discovered_service_info=lambda *_a, **_kw: [],
        async_scanner_devices_by_address=lambda _hass, address, **_kw: list(
            SCANNERS.get(address, ())
        ),
    )
    _mod("bleak", __path__=[])
    _mod("bleak.backends", __path__=[])
    _mod("bleak.backends.device", BLEDevice=object)
    _mod(
        "habluetooth",
        BaseHaRemoteScanner=_RemoteScanner,
        HaScannerRegistration=object,
        get_manager=lambda: MANAGER,
    )

    _mod("truma_proxy_test", __path__=[str(SRC)])
    _mod("truma_proxy_test.truma", __path__=[str(SRC / "truma")])
    _mod("truma_proxy_test.coordinator", TrumaCoordinator=object, TrumaConfigEntry=object)

    def _real(name: str):
        path = SRC / (name.replace(".", "/") + ".py")
        spec = importlib.util.spec_from_file_location(
            f"truma_proxy_test.{name}", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_proxy_test.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _real("const")
    _real("entity")
    state = _real("truma.state")
    proxy = _real("proxy") if (SRC / "proxy.py").exists() else None
    return _real("bt"), _real("binary_sensor"), state, proxy


BT, BINARY_SENSOR, STATE, PROXY = _load()


class _Coordinator:
    unique_id = "Truma iNetX-test"

    def __init__(self, *, connected: bool, proxy_available: bool | None) -> None:
        self.data = types.SimpleNamespace(
            connected=connected,
            flame_status=None,
            raw_params={"EnergySrc.GasLevel": 0},
        )
        self.panel_link_connected = connected
        self.proxy_available = proxy_available


def test_remote_scanner_source_identifies_only_the_proxy_route() -> None:
    address = "62:4A:BD:AD:73:5D"
    SCANNERS[address] = [
        _ScannerDevice(_LocalScanner("hci0")),
        _ScannerDevice(_RemoteScanner("AA:BB:CC:DD:EE:02")),
    ]
    assert BT.async_remote_scanner_source(None, address) == "AA:BB:CC:DD:EE:02"

    SCANNERS[address] = [_ScannerDevice(_LocalScanner("hci0"))]
    assert BT.async_remote_scanner_source(None, address) is None


def test_platform_registers_separate_link_and_proxy_entities() -> None:
    async def _case() -> None:
        added = []
        entry = types.SimpleNamespace(
            runtime_data=_Coordinator(connected=False, proxy_available=True)
        )
        await BINARY_SENSOR.async_setup_entry(None, entry, added.extend)
        assert [type(entity).__name__ for entity in added[:3]] == [
            "TrumaFlameSensor",
            "TrumaConnectionSensor",
            "TrumaProxySensor",
        ]

    asyncio.run(_case())


def test_ble_link_and_proxy_online_states_are_independent() -> None:
    coordinator = _Coordinator(connected=False, proxy_available=True)
    link = BINARY_SENSOR.TrumaConnectionSensor(coordinator)
    proxy = BINARY_SENSOR.TrumaProxySensor(coordinator)
    assert link.is_on is False
    assert proxy.is_on is True
    assert link.available and proxy.available

    # Cached Truma values may remain available between five-minute polls while
    # the physical BLE session itself is already closed.
    coordinator.data.connected = True
    assert link.is_on is False
    coordinator.panel_link_connected = True
    coordinator.proxy_available = False
    assert link.is_on is True
    assert proxy.is_on is False

    coordinator.proxy_available = None
    assert proxy.is_on is None


def test_tracker_follows_the_specific_proxy_registration() -> None:
    assert PROXY is not None, "proxy route tracker is not implemented"
    updates = []
    tracker = PROXY.TrumaProxyTracker(lambda: updates.append(tracker.available))
    tracker.async_setup()
    assert tracker.available is None

    source = "AA:BB:CC:DD:EE:02"
    tracker.remember_source(source)
    assert tracker.source == source
    assert tracker.available is False

    scanner = _RemoteScanner(source)
    MANAGER.sources[source] = scanner
    MANAGER.callback(types.SimpleNamespace(scanner=scanner))
    assert tracker.available is True

    MANAGER.sources.pop(source)
    MANAGER.callback(types.SimpleNamespace(scanner=scanner))
    assert tracker.available is False
    assert updates == [False, True, False]


def test_proxy_sensor_has_bilingual_names_and_an_icon() -> None:
    english = json.loads((SRC / "translations" / "en.json").read_text())
    german = json.loads((SRC / "translations" / "de.json").read_text())
    strings = json.loads((SRC / "strings.json").read_text())
    assert german["entity"]["binary_sensor"]["connection"]["name"] == "BLE-Truma-Verbindung"
    assert german["entity"]["binary_sensor"]["proxy_connection"]["name"] == "BLE-Sender-Verbindung"
    for catalogue in (strings, english):
        assert catalogue["entity"]["binary_sensor"]["connection"]["name"] == "BLE Truma connection"
        assert catalogue["entity"]["binary_sensor"]["proxy_connection"]["name"] == "BLE transmitter connection"
    icons = json.loads((SRC / "icons.json").read_text())
    assert icons["entity"]["binary_sensor"]["proxy_connection"]["default"]


def test_readme_and_lovelace_example_keep_both_statuses_distinct() -> None:
    root_readme = (SRC.parents[1] / "README.md").read_text()
    example = (SRC.parents[1] / "examples" / "lovelace" / "truma-controls.yaml").read_text()
    example_readme = (SRC.parents[1] / "examples" / "lovelace" / "README.md").read_text()
    assert "BLE-Sender-Verbindung" in root_readme
    assert "BLE transmitter connection" in root_readme
    assert "_ble_sender_verbindung" in example
    assert "BLE-Truma-Verbindung" in example_readme
    assert "BLE-Sender-Verbindung" in example_readme


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("proxy status: all checks OK")


if __name__ == "__main__":
    _main()
