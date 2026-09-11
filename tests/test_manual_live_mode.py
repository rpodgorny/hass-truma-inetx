#!/usr/bin/env python3
"""Offline regression checks for manual refresh and timed live mode."""

from __future__ import annotations

import asyncio
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


def _load_coordinator():
    _mod("homeassistant", __path__=[])
    _mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _mod("homeassistant.config_entries", ConfigEntry=dict)
    _mod("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    _mod(
        "homeassistant.helpers",
        __path__=[],
        issue_registry=types.SimpleNamespace(
            async_create_issue=lambda *a, **kw: None,
            async_delete_issue=lambda *a, **kw: None,
            IssueSeverity=types.SimpleNamespace(WARNING="warning"),
        ),
    )
    _mod("homeassistant.helpers.storage", Store=object)
    _mod("bleak_retry_connector", BleakClientWithServiceCache=object)

    class _Coordinator:
        def __class_getitem__(cls, _item):
            return cls

    _mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=_Coordinator)
    _mod("truma_live", __path__=[str(SRC)])
    _mod("truma_live.truma", __path__=[str(SRC / "truma")])
    _mod("truma_live.ble", TrumaBleClient=object, device_from_bluez=None)
    _mod(
        "truma_live.bt",
        async_panel_advertising=lambda *a: False,
        async_remote_scanner_source=lambda *a: None,
        async_resolve_proxy_device=None,
        async_wait_until_heard=None,
    )
    _mod("truma_live.proxy", TrumaProxyTracker=object)

    def _real(name: str, package: str = "truma_live", path: Path = SRC):
        spec = importlib.util.spec_from_file_location(
            f"{package}.{name}", path / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.{name}"] = module
        spec.loader.exec_module(module)
        return module

    _real("const", "truma_live.truma", SRC / "truma")
    _mod(
        "truma_live.truma.protocol",
        build_identity_frames=lambda *a: [],
        build_register_frame=lambda *a: b"",
        build_subscribe_frame=lambda *a: b"",
        build_v3_frame=lambda *a: b"",
        build_write_frame=lambda *a: b"",
    )
    _mod("truma_live.truma.state", TrumaState=object)
    _real("const")
    return _real("coordinator")


COORD = _load_coordinator()


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


class _Client:
    connected = True
    assigned_addr = 0x0501


class _State:
    connected = False
    assigned_addr = None


class _Coord(COORD.TrumaCoordinator):
    """Only the coordinator state touched by the manual-session interface."""

    poll_interval = 300
    unique_id = "Truma iNetX-test"

    def __init__(self, now: float = 0.0) -> None:
        self.clock = _Clock(now)
        self.hass = types.SimpleNamespace(loop=self.clock)
        self._client = None
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._write_ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._stop = False
        self._writes_pending = 0
        self._command_lock = asyncio.Lock()
        self._command_hold_until = 0.0
        self._write_feedback = None
        self._manual_wake_pending = False
        self._manual_hold_request_minutes = None
        self._manual_hold_until = 0.0
        self._manual_release_requested = False
        self.manual_live_minutes = 0
        self._state = _State()
        self._operations = {}
        self._operation_result = ("idle", None, None, None)
        self._command_result_serial = 0
        self._data_revision = 0
        self._manual_requests = {}

    async_request_manual_session = COORD.TrumaCoordinator.async_request_manual_session
    async_end_manual_session = COORD.TrumaCoordinator.async_end_manual_session
    manual_session_active = COORD.TrumaCoordinator.manual_session_active
    _wait_before_retry = COORD.TrumaCoordinator._wait_before_retry
    _reconnect_delay = COORD.TrumaCoordinator._reconnect_delay
    _finish_startup = COORD.TrumaCoordinator._finish_startup
    _client_for_write = COORD.TrumaCoordinator._client_for_write
    async_write_many = COORD.TrumaCoordinator.async_write_many

    async def _request_measurements(self, _client) -> None:
        # A healthy panel answers the periodic on-demand request with a frame.
        self._last_frame = self.clock.now

    async def _run_startup(self, _client) -> None:
        self.clock.now += 20
        self._data_revision += 1

    async def _discover_params(self, _client) -> None:
        self._data_revision += 1

    def async_set_updated_data(self, _state) -> None:
        pass


async def _request_and_connect(minutes: int):
    coord = _Coord()
    task = asyncio.create_task(coord.async_request_manual_session(minutes))
    await asyncio.sleep(0)
    assert coord._wake_event.is_set(), "manual request did not wake poll sleep"
    assert coord._manual_wake_pending
    assert coord._manual_hold_request_minutes == minutes
    coord._client = _Client()
    coord._connected_event.set()
    await task
    return coord


def test_zero_minutes_requests_one_immediate_session() -> None:
    coord = asyncio.run(_request_and_connect(0))
    assert not coord.manual_session_active


def test_connected_request_starts_and_extends_hold_from_now() -> None:
    async def _case():
        coord = _Coord(now=100)
        coord._client = _Client()
        coord._connected_event.set()
        await coord.async_request_manual_session(2)
        assert coord._manual_hold_until == 220
        assert coord.manual_session_active
        coord.clock.now = 110
        await coord.async_request_manual_session(5)
        assert coord._manual_hold_until == 410

    asyncio.run(_case())


def test_request_during_startup_waits_and_does_not_start_timer_early() -> None:
    async def _case():
        coord = _Coord(now=100)
        coord._client = _Client()
        task = asyncio.create_task(coord.async_request_manual_session(2))
        await asyncio.sleep(0)
        assert coord._manual_hold_until == 0
        assert coord._manual_hold_request_minutes == 2
        coord.clock.now = 130
        coord._manual_hold_until = coord.clock.now + 120
        coord._manual_hold_request_minutes = None
        coord._connected_event.set()
        await task
        assert coord._manual_hold_until == 250

    asyncio.run(_case())


def test_duration_must_be_a_whole_number_from_zero_to_999() -> None:
    async def _case():
        coord = _Coord()
        for value in (-1, 1.5, 1000, True):
            try:
                await coord.async_request_manual_session(value)
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"invalid duration accepted: {value!r}")

    asyncio.run(_case())


def test_stop_releases_hold_without_interrupting_a_write() -> None:
    async def _case():
        coord = _Coord(now=10)
        coord._manual_hold_until = 610
        coord._writes_pending = 1
        client = _Client()
        coord._client = client
        await coord.async_end_manual_session()
        assert coord._manual_hold_until == 0
        assert coord._manual_release_requested
        assert coord._writes_pending == 1
        assert client.connected, "stop disconnected underneath an active write"

    asyncio.run(_case())


def test_manual_wake_is_not_lost_between_sessions() -> None:
    async def _case():
        coord = _Coord()
        coord._manual_wake_pending = True
        coord._manual_hold_request_minutes = 3
        await asyncio.wait_for(coord._wait_before_retry(999), timeout=0.1)
        assert coord._manual_hold_request_minutes == 3

    asyncio.run(_case())


def test_live_mode_uses_short_reconnect_delay() -> None:
    coord = _Coord(now=100)
    coord._manual_hold_until = 200
    assert coord._reconnect_delay(connected=False, current=45) == 15
    coord.clock.now = 201
    assert coord._reconnect_delay(connected=False, current=30) == 30


def test_write_waits_until_startup_publishes_write_readiness() -> None:
    async def _case():
        coord = _Coord()
        coord._client = _Client()
        task = asyncio.create_task(coord._client_for_write())
        await asyncio.sleep(0)
        assert not task.done(), "write escaped before registration and identity"

        # Only startup may publish readiness, after discovery has finished.
        coord._write_ready_event.set()
        assert await asyncio.wait_for(task, 0.1) is coord._client
        assert not coord._connected_event.is_set()

    asyncio.run(_case())


def test_hold_timer_starts_only_after_startup_finishes() -> None:
    class _FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds, *_a, **_kw):
            coord.clock.now += seconds

    coord = _Coord()
    coord._state = _State()
    coord._manual_hold_request_minutes = 2
    client = _Client()
    real_asyncio = COORD.asyncio
    COORD.asyncio = _FastAsyncio()
    try:
        asyncio.run(coord._finish_startup(client))
    finally:
        COORD.asyncio = real_asyncio
    # Startup consumed 20 seconds; the two-minute hold begins afterwards.
    assert coord._manual_hold_until == 140
    assert coord.clock.now >= 140


def test_zero_minute_session_disconnects_after_normal_quiet_time() -> None:
    class _FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds, *_a, **_kw):
            coord.clock.now += seconds

    coord = _Coord()
    coord._state = _State()
    coord._manual_hold_request_minutes = 0
    client = _Client()
    real_asyncio = COORD.asyncio
    COORD.asyncio = _FastAsyncio()
    try:
        asyncio.run(coord._finish_startup(client))
    finally:
        COORD.asyncio = real_asyncio
    assert coord._manual_hold_until == 0
    # 20 seconds startup plus the existing four-second quiet dwell.
    assert coord.clock.now == 24


def test_command_hold_overrides_quiet_and_max_dwell_and_extends() -> None:
    coord = _Coord()
    coord._state = _State()
    coord._command_hold_until = 80

    class _FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds, *_a, **_kw):
            coord.clock.now += seconds
            if coord.clock.now == 65:
                # A further command completes 45 seconds into this session.
                coord._command_hold_until = 125

    original = COORD.asyncio
    COORD.asyncio = _FastAsyncio()
    try:
        asyncio.run(coord._finish_startup(_Client()))
    finally:
        COORD.asyncio = original
    assert coord.clock.now == 125


def test_energy_write_rejects_transport_ack_without_fresh_device_feedback() -> None:
    async def case():
        coord = _Coord(now=100)
        coord._state = types.SimpleNamespace(
            validate_write=lambda *a: (True, ""),
            get_command_dest=lambda *a: 0x0201,
        )
        class Client(_Client):
            async def send(self, frame):
                if coord.clock.now == 140:
                    coord._write_feedback[(0x0201, "AirHeating", "TgtTemp")] = 210
                return True
        coord._client = Client()
        coord._write_ready_event.set()
        original = COORD._WRITE_FEEDBACK_TIMEOUT
        COORD._WRITE_FEEDBACK_TIMEOUT = 0
        try:
            try:
                await coord.async_write_many([("EnergySrc", "DieselLevel", 1)], confirm=True)
            except RuntimeError as exc:
                assert "did not confirm" in str(exc)
            else:
                raise AssertionError("transport ACK was mistaken for device confirmation")
        finally:
            COORD._WRITE_FEEDBACK_TIMEOUT = original
        assert coord._writes_pending == 0
        assert coord._command_hold_until == 160
        assert coord._write_feedback is None
        coord.clock.now = 140
        await coord.async_write_many([("AirHeating", "TgtTemp", 210)])
        assert coord._command_hold_until == 200
    asyncio.run(case())


def test_energy_write_accepts_fresh_device_values_and_holds_after_completion() -> None:
    async def case():
        coord = _Coord(now=100)
        coord._state = types.SimpleNamespace(
            validate_write=lambda *a: (True, ""),
            get_command_dest=lambda *a: 0x0201,
        )
        class Client(_Client):
            async def send(self, frame):
                # Device notification after sending, never an optimistic
                # assignment to the entity's current state.
                coord._write_feedback[(0x0201, "EnergySrc", "DieselLevel")] = 1
                coord._write_feedback[(0x0201, "EnergySrc", "ElectricLevel")] = 1
                return True
        coord._client = Client()
        coord._write_ready_event.set()
        await coord.async_write_many([
            ("EnergySrc", "DieselLevel", 1),
            ("EnergySrc", "ElectricLevel", 1),
        ], confirm=True)
        assert coord._command_hold_until == 160
        assert coord._writes_pending == 0
    asyncio.run(case())


def test_energy_write_retries_a_sleeping_heater_but_is_bounded() -> None:
    async def case():
        coord = _Coord(now=100)
        coord._state = types.SimpleNamespace(
            validate_write=lambda *a: (True, ""),
            get_command_dest=lambda *a: 0x0201,
        )
        class Client(_Client):
            sends = 0
            async def send(self, frame):
                self.sends += 1
                if self.sends == 3:
                    coord._write_feedback[(0x0201, "EnergySrc", "DieselLevel")] = 1
                return self.sends >= 3
        coord._client = Client()
        coord._write_ready_event.set()
        original = COORD._WRITE_FEEDBACK_TIMEOUT
        COORD._WRITE_FEEDBACK_TIMEOUT = 0
        try:
            await coord.async_write_many([("EnergySrc", "DieselLevel", 1)], confirm=True)
        finally:
            COORD._WRITE_FEEDBACK_TIMEOUT = original
        assert coord._client.sends == 4  # two writes, each followed by readback
        assert coord._writes_pending == 0
    asyncio.run(case())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("manual live mode: all checks OK")


if __name__ == "__main__":
    _main()
