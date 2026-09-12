#!/usr/bin/env python3
"""Offline checks for the Home Assistant manual-session controls."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "truma_inetx"


def _mod(name: str, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _load():
    class _CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

        async def async_added_to_hass(self) -> None:
            pass

    class _NumberEntity:
        @property
        def native_value(self):
            return self._attr_native_value

        def async_write_ha_state(self) -> None:
            pass

    class _RestoreNumber(_NumberEntity):
        async def async_get_last_number_data(self):
            return getattr(self, "_restored", None)

    class _ButtonEntity:
        pass

    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.const", UnitOfTime=types.SimpleNamespace(MINUTES="min"))
    _mod("homeassistant.helpers", __path__=[])
    _mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _mod("homeassistant.helpers.entity", Entity=object)
    _mod("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
    _mod("homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity)
    _mod("homeassistant.components", __path__=[])
    _mod(
        "homeassistant.components.number",
        NumberEntity=_NumberEntity,
        NumberMode=types.SimpleNamespace(SLIDER="slider", BOX="box"),
        RestoreNumber=_RestoreNumber,
    )
    _mod("homeassistant.components.button", ButtonEntity=_ButtonEntity)

    _mod("truma_controls", __path__=[str(SRC)])
    _mod("truma_controls.truma", __path__=[str(SRC / "truma")])

    class _Coordinator:
        def __class_getitem__(cls, _item):
            return cls

    _mod(
        "truma_controls.coordinator",
        TrumaCoordinator=_Coordinator,
        TrumaConfigEntry=object,
    )

    def _real(name: str):
        spec = importlib.util.spec_from_file_location(
            f"truma_controls.{name}", SRC / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"truma_controls.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _real("const")
    _real("entity")
    return _real("number"), _real("button")


NUMBER, BUTTON = _load()


class _Coordinator:
    unique_id = "Truma iNetX-test"
    data = types.SimpleNamespace(connected=False, fan_level=0)

    def __init__(self) -> None:
        self.manual_live_minutes = 0
        self.requests: list[int] = []
        self.stops = 0

    async def async_request_manual_session(self, minutes: int) -> None:
        self.requests.append(minutes)

    async def async_end_manual_session(self) -> None:
        self.stops += 1


def test_platforms_create_three_manual_controls() -> None:
    async def _case():
        coordinator = _Coordinator()
        entry = types.SimpleNamespace(runtime_data=coordinator)
        numbers: list = []
        buttons: list = []
        await NUMBER.async_setup_entry(None, entry, numbers.extend)
        await BUTTON.async_setup_entry(None, entry, buttons.extend)
        assert [type(item).__name__ for item in numbers] == [
            "TrumaFanLevel",
            "TrumaManualLiveMinutes",
        ]
        assert [type(item).__name__ for item in buttons] == [
            "TrumaManualSyncButton",
            "TrumaManualStopButton",
        ]

    asyncio.run(_case())


def test_duration_is_a_restored_box_number_from_zero_to_999() -> None:
    entity = NUMBER.TrumaManualLiveMinutes(_Coordinator())
    assert entity._attr_native_min_value == 0
    assert entity._attr_native_max_value == 999
    assert entity._attr_native_step == 1
    assert entity._attr_mode == "box"
    assert entity._attr_native_unit_of_measurement == "min"
    assert entity.available, "local control vanished while BLE was disconnected"


def test_duration_defaults_to_zero_and_restores_a_valid_value() -> None:
    async def _case():
        coordinator = _Coordinator()
        entity = NUMBER.TrumaManualLiveMinutes(coordinator)
        await entity.async_added_to_hass()
        assert entity.native_value == 0
        entity._restored = types.SimpleNamespace(native_value=37)
        await entity.async_added_to_hass()
        assert entity.native_value == 37
        assert coordinator.manual_live_minutes == 37

    asyncio.run(_case())


def test_number_rejects_fractional_and_out_of_range_values() -> None:
    async def _case():
        entity = NUMBER.TrumaManualLiveMinutes(_Coordinator())
        for value in (-1, 1.5, 1000):
            try:
                await entity.async_set_native_value(value)
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid duration accepted: {value!r}")

    asyncio.run(_case())


def test_buttons_forward_start_and_stop() -> None:
    async def _case():
        coordinator = _Coordinator()
        coordinator.manual_live_minutes = 12
        start = BUTTON.TrumaManualSyncButton(coordinator)
        stop = BUTTON.TrumaManualStopButton(coordinator)
        assert start.available and stop.available
        await start.async_press()
        await stop.async_press()
        assert coordinator.requests == [12]
        assert coordinator.stops == 1

    asyncio.run(_case())


def test_platform_metadata_is_registered() -> None:
    init_text = (SRC / "__init__.py").read_text()
    assert "Platform.BUTTON" in init_text

    strings = json.loads((SRC / "strings.json").read_text())
    english = json.loads((SRC / "translations" / "en.json").read_text())
    icons = json.loads((SRC / "icons.json").read_text())
    for catalogue in (strings, english):
        assert "manual_live_minutes" in catalogue["entity"]["number"]
        assert "manual_sync" in catalogue["entity"]["button"]
        assert "manual_stop" in catalogue["entity"]["button"]
    assert "manual_live_minutes" in icons["entity"]["number"]
    assert "manual_sync" in icons["entity"]["button"]
    assert "manual_stop" in icons["entity"]["button"]


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("manual live entities: all checks OK")


if __name__ == "__main__":
    _main()
