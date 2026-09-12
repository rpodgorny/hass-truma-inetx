#!/usr/bin/env python3
"""Offline operation lifecycle tests: real coordinator, controlled BLE boundary."""
import asyncio
import types
from contextlib import contextmanager
from unittest.mock import patch

from test_manual_live_mode import COORD, _Coord, _Client


def ready():
    coord = _Coord()
    coord._state = types.SimpleNamespace(
        validate_write=lambda *a: (True, ""),
        get_command_dest=lambda *a: 0x0201,
    )
    return coord


def test_write_status_precedes_connection_and_cancel_is_terminal():
    async def case():
        c = ready()
        task = asyncio.create_task(c.async_write_many([("RoomClimate", "Mode", 3)]))
        await asyncio.sleep(0)
        assert getattr(c, "operation_state", None) == "changing"
        assert c.operation_attributes == {"action": "hvac_mode", "target": "heat", "error": None}
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert c.operation_state == "error"
        assert c.operation_attributes["error"]
        assert c._writes_pending == 0
    asyncio.run(case())


def test_queued_cancel_does_not_clear_active_write():
    async def case():
        c = ready()
        first = asyncio.create_task(c.async_write_many([("RoomClimate", "Mode", 3)]))
        await asyncio.sleep(0)
        second = asyncio.create_task(c.async_write_many([("EnergySrc", "ElectricLevel", 1)]))
        await asyncio.sleep(0)
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass
        assert getattr(c, "operation_state", None) == "changing"
        assert c.operation_attributes["action"] == "hvac_mode"
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        assert c.operation_state == "error"
    asyncio.run(case())


def test_manual_sync_starts_before_wait_and_failure_persists():
    async def case():
        c = ready()
        task = asyncio.create_task(c.async_request_manual_session(0))
        await asyncio.sleep(0)
        assert getattr(c, "operation_state", None) == "syncing"
        assert c.operation_attributes["action"] == "sync"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert c.operation_state == "error"
        assert c._manual_hold_request_minutes is None
    asyncio.run(case())


def test_single_write_requires_panel_confirmation():
    async def case():
        c = ready()
        class Client(_Client):
            async def send(self, frame):
                return True
        c._client = Client()
        c._write_ready_event.set()
        original = COORD._WRITE_FEEDBACK_TIMEOUT
        COORD._WRITE_FEEDBACK_TIMEOUT = 0
        try:
            try:
                await c.async_write("RoomClimate", "Mode", 3)
            except RuntimeError:
                pass
            else:
                raise AssertionError("ACK alone reported successful operation")
        finally:
            COORD._WRITE_FEEDBACK_TIMEOUT = original
        assert c.operation_state == "error"
    asyncio.run(case())


def test_background_sync_cannot_erase_command_failure():
    c = ready()
    command = c._begin_operation("energy_source", "electric")
    sync = c._begin_operation("sync")  # reconnect caused by that command
    c._end_operation(command, "Panel did not confirm")
    c._end_operation(sync)
    assert c.operation_state == "error"
    assert c.operation_attributes["action"] == "energy_source"


async def command_failure_overlapping_sync(sync_finishes_first):
    """Exercise the public write path across both reconnect completion orders."""
    c = ready()
    entered = asyncio.Event()
    release = asyncio.Event()

    class Client(_Client):
        async def send(self, frame):
            entered.set()
            await release.wait()
            raise RuntimeError("Panel link lost")

    c._client = Client()
    c._write_ready_event.set()
    command = asyncio.create_task(c.async_write("RoomClimate", "Mode", 3))
    await entered.wait()
    sync = c._begin_operation("sync")
    if sync_finishes_first:
        c._end_operation(sync)
    assert c.operation_state == "changing"
    release.set()
    try:
        await command
    except RuntimeError as exc:
        assert str(exc) == "Panel link lost"
    else:
        raise AssertionError("failed write returned success")
    assert c.operation_state == "error"
    expected = {"action": "hvac_mode", "target": "heat", "error": "Panel link lost"}
    assert c.operation_attributes == expected
    if not sync_finishes_first:
        c._end_operation(sync)
    assert c.operation_state == "error"
    assert c.operation_attributes == expected

    # Further automatic retries must neither hide nor erase the error.
    for failure in (None, "Background connection failed"):
        retry = c._begin_operation("sync")
        assert c.operation_state == "error"
        assert c.operation_attributes == expected
        c._end_operation(retry, failure)
        assert c.operation_state == "error"
        assert c.operation_attributes == expected

    # A new user command takes display priority and confirmed success clears
    # the previous error, even with background sync still pending.
    class ConfirmingClient(_Client):
        async def send(self, frame):
            assert c.operation_state == "changing"
            assert c.operation_attributes == {"action": "hvac_mode", "target": "off", "error": None}
            c._write_feedback[(0x0201, "RoomClimate", "Mode")] = 0
            return True

    retry = c._begin_operation("sync")
    c._client = ConfirmingClient()
    await c.async_write("RoomClimate", "Mode", 0)
    assert c.operation_state == "syncing"
    assert c.operation_attributes["error"] is None
    c._end_operation(retry)
    assert c.operation_state == "idle"


def test_successful_reconnect_before_write_failure_cannot_suppress_error():
    asyncio.run(command_failure_overlapping_sync(True))


def test_pending_reconnect_cannot_hide_write_failure():
    asyncio.run(command_failure_overlapping_sync(False))


def test_initial_and_periodic_session_start_before_dial_and_end_after_startup():
    async def case():
        c = ready()
        c._avoid = set()
        c._last_addr = None
        seen = []
        async def connect():
            seen.append(c.operation_state)
            assert c.operation_attributes["action"] == "sync"
            # The actual startup-completion path must release sync before hold.
            c._state = types.SimpleNamespace(connected=False, assigned_addr=None)
            c._stop = True
            await c._finish_startup(_Client())
            assert c.operation_state == "idle"
            return True
        async def disconnect():
            pass
        c._connect_and_run = connect
        c._disconnect_client = disconnect
        for _ in range(2):
            c._stop = False
            await c._run()
        assert seen == ["syncing", "syncing"]
    asyncio.run(case())


def test_queued_write_takes_over_after_first_confirmation():
    async def case():
        c = ready()
        gate = asyncio.Event()
        class Client(_Client):
            async def send(self, frame):
                await gate.wait()
                c._write_feedback[(0x0201, "EnergySrc", "ElectricLevel")] = 1
                return True
        c._client = Client()
        c._write_ready_event.set()
        first = asyncio.create_task(c.async_write_many([("RoomClimate", "Mode", 3)]))
        await asyncio.sleep(0)
        second = asyncio.create_task(c.async_write_many([("EnergySrc", "ElectricLevel", 1)]))
        await asyncio.sleep(0)
        assert c.operation_attributes["action"] == "hvac_mode"
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        assert c.operation_state == "changing"
        assert c.operation_attributes["action"] == "electric_heating"
        gate.set()
        await second
        assert c.operation_state == "idle"
    asyncio.run(case())


def test_connected_manual_refresh_reads_parameters_before_idle():
    async def case():
        c = ready()
        c._client = _Client()
        c._connected_event.set()
        discovery = []
        async def discover(client):
            assert c.operation_state == "syncing"
            discovery.append(client)
            c._data_revision = getattr(c, "_data_revision", 0) + 1
        c._discover_params = discover
        await c.async_request_manual_session(0)
        assert discovery == [c._client], "connected refresh only requested optional measurements"
        assert c.operation_state == "idle"
        assert c._manual_hold_until == 0
    asyncio.run(case())


def test_sync_handshake_without_data_fails_instead_of_idle():
    async def case():
        c = ready()
        c._state = types.SimpleNamespace(connected=False, assigned_addr=None)
        c._stop = True
        async def handshake_only(client):
            pass
        c._run_startup = handshake_only
        try:
            await c._finish_startup(_Client())
        except RuntimeError:
            pass
        else:
            raise AssertionError("registration without parameter data counted as sync")
        assert not c._write_ready_event.is_set()
    asyncio.run(case())


def test_explicit_unconfirmed_write_still_waits_for_panel():
    async def case():
        c = ready()
        class Client(_Client):
            async def send(self, frame):
                return True
        c._client = Client()
        c._write_ready_event.set()
        original = COORD._WRITE_FEEDBACK_TIMEOUT
        COORD._WRITE_FEEDBACK_TIMEOUT = 0
        try:
            try:
                await c.async_write_many([("RoomClimate", "Mode", 3)], confirm=False)
            except RuntimeError:
                pass
            else:
                raise AssertionError("explicit confirm=False bypassed panel confirmation")
        finally:
            COORD._WRITE_FEEDBACK_TIMEOUT = original
        assert c.operation_state == "error"
    asyncio.run(case())


def test_stop_cancels_pending_manual_refresh_without_resurrecting_hold():
    async def case():
        c = ready()
        task = asyncio.create_task(c.async_request_manual_session(5))
        await asyncio.sleep(0)
        await c.async_end_manual_session()
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        except RuntimeError as exc:
            assert "cancelled" in str(exc)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("stop left manual request waiting for a future connection")
        else:
            raise AssertionError("cancelled refresh reported success")
        assert c.operation_state == "error"
        assert c._manual_hold_request_minutes is None
        assert c._manual_release_requested
        assert not c._wake_event.is_set()
        c._stop = True
        c._state = types.SimpleNamespace(connected=False, assigned_addr=None)
        await c._finish_startup(_Client())
        assert c._manual_hold_until == 0
        assert c._manual_release_requested
    asyncio.run(case())


def test_energy_transition_uses_real_notifications_and_logs_only_confirmation():
    # Reuse the offline HA platform loader, but run the real coordinator and
    # state parser; only the BLE transport is replaced.
    from test_energy_entities import STATE, SELECT

    async def case(fail):
        c = ready()
        c._state = STATE.TrumaState()
        c.data = c._state
        c._state.update("EnergySrc", "DieselLevel", 1, 0x0201)
        c._state.update("EnergySrc", "ElectricLevel", 0, 0x0201)
        entity = SELECT.TrumaEnergySourceSelect(c)
        events = []
        entity.entity_id = "select.test_energy"
        entity.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(language="en"),
            bus=types.SimpleNamespace(async_fire=lambda *a: events.append(a)),
        )
        observed = []
        class Client(_Client):
            async def send(self, frame):
                assert c.operation_state == "changing"
                assert entity.current_option == "changing"
                assert not events
                if isinstance(frame, tuple):
                    topic, param, value = frame
                    if not (fail and param == "DieselLevel"):
                        c._on_frame({
                            "src": 0x0201, "control_raw": 0x03, "sub_type": 0x00,
                            "cbor": {"tn": topic, "pn": param, "v": value},
                        })
                    observed.append(entity.current_option)
                return True
        c._client = Client()
        c._write_ready_event.set()
        original_frame = COORD.build_write_frame
        original_timeout = COORD._WRITE_FEEDBACK_TIMEOUT
        COORD.build_write_frame = lambda src, dest, topic, param, value: (topic, param, value)
        COORD._WRITE_FEEDBACK_TIMEOUT = 0
        try:
            try:
                await entity.async_select_option("electric")
            except RuntimeError:
                assert fail
            else:
                assert not fail
        finally:
            COORD.build_write_frame = original_frame
            COORD._WRITE_FEEDBACK_TIMEOUT = original_timeout
        assert observed and set(observed) == {"changing"}
        assert entity.current_option == ("hybrid" if fail else "electric")
        assert c.operation_state == ("error" if fail else "idle")
        assert len(events) == (0 if fail else 1)
        assert not c.energy_source_changing
    asyncio.run(case(False))
    asyncio.run(case(True))


def test_connected_one_shot_refresh_keeps_poll_open_until_read_finishes():
    c = ready()
    c._state = types.SimpleNamespace(connected=False, assigned_addr=None)
    c._manual_requests[1] = asyncio.Event()
    class FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds):
            c.clock.now += seconds
            if c.clock.now >= 30:
                c._manual_requests.clear()
    original = COORD.asyncio
    COORD.asyncio = FastAsyncio()
    try:
        asyncio.run(c._finish_startup(_Client()))
    finally:
        COORD.asyncio = original
    assert c.clock.now >= 30, "poll disconnected while manual discovery was pending"


def test_connected_refresh_failure_and_cancel_release_owned_hold():
    async def case(cancel):
        c = ready()
        c._client = _Client()
        c._connected_event.set()
        entered = asyncio.Event()
        gate = asyncio.Event()
        async def discover(client):
            entered.set()
            await gate.wait()
            raise RuntimeError("Discovery failed")
        c._discover_params = discover
        task = asyncio.create_task(c.async_request_manual_session(999))
        await entered.wait()
        if cancel:
            task.cancel()
        else:
            gate.set()
        await asyncio.gather(task, return_exceptions=True)
        assert c.operation_state == "error"
        assert c._manual_hold_until == 0, "failed refresh left a 999 minute hold"
        assert not c.manual_session_active
    asyncio.run(case(False))
    asyncio.run(case(True))


def test_older_refresh_failure_cannot_clear_newer_successful_hold():
    async def case():
        c = ready()
        c._client = _Client()
        c._connected_event.set()
        entered = asyncio.Event()
        gate = asyncio.Event()
        calls = 0
        async def discover(client):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await gate.wait()
                raise RuntimeError("Old discovery failed")
            c._data_revision += 1
        c._discover_params = discover
        old = asyncio.create_task(c.async_request_manual_session(999))
        await entered.wait()
        await c.async_request_manual_session(2)
        assert c._manual_hold_until == 120
        gate.set()
        await asyncio.gather(old, return_exceptions=True)
        assert c._manual_hold_until == 120
    asyncio.run(case())


@contextmanager
def water_panel(responses, *, initial_active=0, source=0x0201, delay=0,
                after_reply=None, discovery=False):
    """Real select, coordinator and notification handling; no BLE/HA connection.

    Literal responses model the appliance boundary. None means transport ACK
    only, not parameter feedback. A virtual clock keeps missing-ACK tests fast.
    """
    from test_energy_entities import STATE, SELECT

    c = _Coord()
    c._state = STATE.TrumaState()
    c.data = c._state
    c._state.update("WaterHeating", "Active", initial_active, 0x0201)
    c._state.update("WaterHeating", "Mode", 0, 0x0201)
    c._state.update("WaterHeating", "Temp", 451, 0x0201)
    entity = SELECT.TrumaWaterModeSelect(c)
    writes = []
    pending = []

    def notify(topic, param, value):
        cbor = {"tn": topic, "pn": param, "v": value}
        if discovery:
            cbor = {"topics": [{"tn": topic, "parameters": [{"pn": param, "v": value}]}]}
        c._on_frame({
            "src": source, "control_raw": 0x03,
            "sub_type": 0x84 if discovery else 0x00, "cbor": cbor,
        })

    class Client(_Client):
        async def send(self, frame):
            if isinstance(frame, tuple):
                dest, topic, param, value = frame
                writes.append((dest, topic, param, value))
                response = responses.get((topic, param), value)
                if response is not None:
                    if delay:
                        pending.append([delay, topic, param, response])
                    else:
                        notify(topic, param, response)
                if after_reply is not None:
                    after_reply(topic, param, value, notify)
            return True

    class FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, seconds):
            c.clock.now += seconds
            for item in list(pending):
                item[0] -= 1
                if item[0] == 0:
                    pending.remove(item)
                    notify(*item[1:])
            await asyncio.sleep(0)

    c._client = Client()
    c._write_ready_event.set()
    with patch.object(COORD, "asyncio", FastAsyncio()), patch.object(
        COORD, "build_write_frame",
        lambda src, dest, topic, param, value: (dest, topic, param, value),
    ):
        yield c, entity, writes


async def operation_error(operation):
    """Capture only the HA error (RuntimeError in this offline HA harness)."""
    try:
        await operation
    except RuntimeError as exc:
        return str(exc)
    return None


def test_water_enable_accepts_fresh_idle_when_target_already_reached():
    # Break caught: requiring Active==1 rejects a fresh enabled-but-idle value
    # and never reaches the user's requested water-temperature mode.
    async def case():
        with water_panel({("WaterHeating", "Active"): 2}) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Eco (40 °C)"))
            assert error is None, f"Idle water setting falsely failed: {error}"
            assert writes == [(0x0201, "WaterHeating", "Active", 1),
                              (0x0201, "WaterHeating", "Mode", 0)]
            assert c._state.water_active == 2
            assert entity.current_option == "Eco (40 °C)"
            assert c.operation_state == "idle"
            assert c._write_feedback is None
            assert c._writes_pending == 0
    asyncio.run(case())


def test_water_delayed_idle_confirmation_finishes_before_timeout():
    # Break caught: fixing only the post-wait check still waits the full
    # timeout when a valid Idle notification arrives during the wait loop.
    async def case():
        with water_panel({("WaterHeating", "Active"): 2}, delay=2) as (c, _, writes):
            error = await operation_error(c.async_write("WaterHeating", "Active", 1))
            assert error is None, error
            assert c.clock.now < 1, "valid Idle feedback waited until timeout"
            assert writes == [(0x0201, "WaterHeating", "Active", 1)]
            assert c.operation_state == "idle"
    asyncio.run(case())


def test_water_modes_still_require_the_selected_temperature():
    async def case():
        for active in (1, 2):
            for option, mode in [("Eco (40 °C)", 0), ("Comfort (60 °C)", 1),
                                 ("Hot (70 °C)", 2)]:
                with water_panel({("WaterHeating", "Active"): active}) as (c, entity, writes):
                    error = await operation_error(entity.async_select_option(option))
                    assert error is None, (option, active, error)
                    assert writes == [(0x0201, "WaterHeating", "Active", 1),
                                      (0x0201, "WaterHeating", "Mode", mode)]
                    assert entity.current_option == option
                    assert c.operation_state == "idle"
    asyncio.run(case())


def test_water_off_accepts_only_fresh_off_not_enabled_idle():
    async def case():
        for feedback in (0, 1, 2):
            with water_panel({("WaterHeating", "Active"): feedback}, initial_active=2) as (c, entity, writes):
                error = await operation_error(entity.async_select_option("off"))
                if feedback == 0:
                    assert error is None, error
                    assert entity.current_option == "off"
                    assert c.operation_state == "idle"
                    assert writes == [(0x0201, "WaterHeating", "Active", 0)]
                else:
                    assert error and "did not confirm WaterHeating.Active=0" in error
                    assert c.operation_state == "error"
                    assert writes == [(0x0201, "WaterHeating", "Active", 0)] * 3
    asyncio.run(case())


def test_water_enable_rejects_off_unknown_and_missing_feedback():
    async def case():
        for feedback in (0, 3, None):
            with water_panel({("WaterHeating", "Active"): feedback}) as (c, entity, writes):
                error = await operation_error(entity.async_select_option("Comfort (60 °C)"))
                assert error and "did not confirm WaterHeating.Active=1" in error
                assert c.operation_state == "error"
                assert writes == [(0x0201, "WaterHeating", "Active", 1)] * 3
                assert c._write_feedback is None
                assert c._writes_pending == 0
                assert c._command_hold_until == c.clock.now + 60
    asyncio.run(case())


def test_water_cached_idle_is_not_a_new_confirmation():
    async def case():
        with water_panel({("WaterHeating", "Active"): None}, initial_active=2) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Eco (40 °C)"))
            assert error and "did not confirm" in error
            assert entity.current_option == "Eco (40 °C)"
            assert c.operation_state == "error", "cached option hid the failed new command"
            assert len(writes) == 3
    asyncio.run(case())


def test_water_idle_from_wrong_device_is_not_confirmation():
    async def case():
        with water_panel({("WaterHeating", "Active"): 2}, source=0x0101) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Eco (40 °C)"))
            assert error and "did not confirm" in error
            assert c.operation_state == "error"
            assert len(writes) == 3
    asyncio.run(case())


def test_water_idle_can_be_confirmed_by_fresh_discovery():
    async def case():
        with water_panel({("WaterHeating", "Active"): 2}, discovery=True) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Eco (40 °C)"))
            assert error is None, error
            assert c.operation_state == "idle"
            assert entity.current_option == "Eco (40 °C)"
            assert c._state.water_active == 2
            assert writes == [(0x0201, "WaterHeating", "Active", 1),
                              (0x0201, "WaterHeating", "Mode", 0)]
    asyncio.run(case())


def test_water_discovery_from_wrong_device_is_not_confirmation():
    async def case():
        with water_panel({("WaterHeating", "Active"): 2}, source=0x0101,
                         discovery=True) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Eco (40 °C)"))
            assert error and "did not confirm" in error
            assert c.operation_state == "error"
            assert len(writes) == 3
    asyncio.run(case())


def test_water_idle_does_not_confirm_the_wrong_temperature_mode():
    async def case():
        with water_panel({("WaterHeating", "Active"): 2,
                          ("WaterHeating", "Mode"): 2}) as (c, entity, writes):
            error = await operation_error(entity.async_select_option("Comfort (60 °C)"))
            assert error and "did not confirm WaterHeating.Mode=1" in error
            assert c.operation_state == "error"
            assert writes == [(0x0201, "WaterHeating", "Active", 1)] + [
                (0x0201, "WaterHeating", "Mode", 1)] * 3
    asyncio.run(case())


def test_idle_exception_does_not_relax_other_parameters():
    async def case():
        for topic, param in [("EnergySrc", "ElectricLevel"), ("AirHeating", "Mode"),
                             ("AirCirculation", "Active"), ("WaterHeating", "Mode")]:
            with water_panel({(topic, param): 2}) as (c, _, writes):
                error = await operation_error(c.async_write(topic, param, 1))
                assert error and f"did not confirm {topic}.{param}=1" in error
                assert c.operation_state == "error"
                assert len(writes) == 3
    asyncio.run(case())


def test_water_transaction_can_settle_from_active_to_idle():
    # Break caught: final multi-parameter validation must use the same meaning
    # of enabled as the per-write wait, even if the heater stops needing heat.
    async def case():
        def idle_after_mode(topic, param, value, notify):
            if (topic, param) == ("WaterHeating", "Mode"):
                notify("WaterHeating", "Active", 2)
        with water_panel({}, after_reply=idle_after_mode) as (c, _, writes):
            error = await operation_error(c.async_write_many([
                ("WaterHeating", "Active", 1), ("WaterHeating", "Mode", 0)]))
            assert error is None, error
            assert c._state.water_active == 2
            assert c.operation_state == "idle"
            assert len(writes) == 2
    asyncio.run(case())


def test_water_transaction_cannot_succeed_if_switched_off_again():
    async def case():
        def off_after_mode(topic, param, value, notify):
            if (topic, param) == ("WaterHeating", "Mode"):
                notify("WaterHeating", "Active", 0)
        with water_panel({}, after_reply=off_after_mode) as (c, _, writes):
            error = await operation_error(c.async_write_many([
                ("WaterHeating", "Active", 1), ("WaterHeating", "Mode", 0)]))
            assert error and "did not retain" in error
            assert c.operation_state == "error"
            assert c._state.water_active == 0
            assert len(writes) == 2
    asyncio.run(case())


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
