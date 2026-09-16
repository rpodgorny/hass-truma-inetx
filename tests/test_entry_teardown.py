#!/usr/bin/env python3
"""Offline check that unloading the entry leaves nothing of ours running.

No hardware, no Home Assistant install: the HA and bleak imports are stubbed
so the real ``async_stop`` and the real ``async_unload_entry``/
``async_setup_entry`` run.

Why this exists: reloading the config entry used to leave the integration
unloaded with the BLE client still connected -- measured on the van,
2026-09-15, and reachable by enabling or disabling a single entity, because
Home Assistant reloads the entry on that by itself. An orphaned link holds one
of the panel's ~4 connection slots, so the entry could not be reloaded by hand
either; only restarting Home Assistant cleared it.

What it pins:

1. the session task is ended *before* the link is closed -- a task cancelled
   mid-flight cannot run its own teardown, so anything it held would be left
   connected,
2. a session that will not end is cancelled, and the stop still returns rather
   than hanging the unload that is waiting on it,
3. a handed-off pairing link that no session ever adopted is closed too, and
   a session cancelled by something else does not stop the rest of the
   teardown -- while a cancellation aimed at the stop itself is not swallowed,
4. the session is stopped even when a platform refuses to unload, and the
   refusal is still reported,
5. a setup that fails after the session was launched stops it on the way out,
   since Home Assistant never calls unload for an entry whose setup raised.

Run: ``python3 tests/test_entry_teardown.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

stubs.install_homeassistant()
stubs.mod("homeassistant.components.bluetooth",
          async_ble_device_from_address=lambda *a, **kw: object())
stubs.mod("homeassistant.components.frontend", add_extra_js_url=lambda *a: None)
stubs.mod("homeassistant.components.http", StaticPathConfig=object)
stubs.stub_transport()
# Only __init__.py asks for this one, so the shared stub does not carry it.
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
# The session sequence and the frame builders need the protocol library, and
# none of this is about either: nothing below sends a frame.
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          StartupFailed=RuntimeError, handle_frame=None)
stubs.mod("truma_pkg.truma.protocol", build_write_frame=None)

COORD = stubs.load("coordinator")


async def _close_link(client, _label) -> None:
    """Stand in for ble.close_link, which the coordinator imported by name.

    The real one is bounded and tests/test_transport_ack_order.py pins that
    against a link whose disconnect never returns. What matters here is only
    that the coordinator hands it the link it was still holding.
    """
    await client.disconnect()


COORD.close_link = _close_link
# Loaded under a name of its own: this is the package's __init__.py, and the
# relative imports in it resolve against truma_pkg either way.
ENTRY = stubs.load("__init__")

PANEL = "Truma iNetX-FFB4D1"


class _Link:
    """A BLE link that records being closed, and can refuse to close."""

    def __init__(self, *, hangs: bool = False) -> None:
        self.closed = 0
        self.hangs = hangs

    async def disconnect(self) -> None:
        if self.hangs:
            await asyncio.sleep(3600)
        self.closed += 1


class _Coord:
    """Carries only what the stop path touches."""

    unique_id = PANEL

    def __init__(self, *, initial: _Link | None = None) -> None:
        self._stop = False
        self._stop_event = asyncio.Event()
        self._client: _Link | None = None
        self._initial_client = initial
        self._session_task: asyncio.Task | None = None
        self.log: list[str] = []

    async_stop = COORD.TrumaCoordinator.async_stop
    _stop_session_task = COORD.TrumaCoordinator._stop_session_task
    _release_initial_client = COORD.TrumaCoordinator._release_initial_client

    async def _disconnect_client(self) -> None:
        """The real one, minus the logging: record the order, not the words."""
        client = self._client
        self._client = None
        if client is not None:
            self.log.append("closed the session link")
            await client.disconnect()


def test_the_session_ends_before_its_link_is_closed() -> None:
    async def run() -> _Coord:
        c = _Coord()
        link = _Link()
        c._client = link

        async def session() -> None:
            try:
                while not c._stop:
                    await asyncio.sleep(0)
            finally:
                # A real session closes its own link in its finally. That only
                # works while the task is alive: the stop has to wait for this,
                # not cancel it and close the link behind its back.
                c.log.append("session ended")
                await c._disconnect_client()

        c._session_task = asyncio.get_running_loop().create_task(session())
        await asyncio.sleep(0)
        await c.async_stop()
        assert c._session_task is None, "the stopped task is still tracked"
        assert link.closed == 1, f"link closed {link.closed} times, wanted once"
        assert c.log == ["session ended", "closed the session link"], (
            f"teardown ran out of order: {c.log}"
        )
        return c

    asyncio.run(run())


def test_a_session_that_will_not_end_is_cancelled() -> None:
    async def run() -> None:
        c = _Coord()
        # Every wait in the real loop watches the stop event; this stands in
        # for the one that does not -- a task parked inside a connect attempt.
        async def wedged() -> None:
            await asyncio.sleep(3600)

        c._session_task = asyncio.get_running_loop().create_task(wedged())
        await asyncio.sleep(0)
        task = c._session_task

        was = COORD._SESSION_EXIT_TIMEOUT
        COORD._SESSION_EXIT_TIMEOUT = 0.05
        try:
            await c.async_stop()
        finally:
            # Restored rather than left short: the bound is what every other
            # check here waits on, and a test that shortens it for the rest of
            # the file would hide a stop that had become slow.
            COORD._SESSION_EXIT_TIMEOUT = was

        assert task.cancelled(), "a wedged session survived the stop"
        assert c._stop is True

    asyncio.run(run())


def test_a_session_cancelled_by_something_else_still_leaves_a_clean_stop() -> None:
    async def run() -> None:
        c = _Coord(initial=_Link())
        link = _Link()
        c._client = link

        async def session() -> None:
            await asyncio.sleep(3600)

        task = asyncio.get_running_loop().create_task(session())
        c._session_task = task
        await asyncio.sleep(0)
        # Somebody else cancels it while the stop is waiting -- Home Assistant
        # shutting down over the top of an unload, say. That is the session's
        # cancellation, not ours, so the stop carries on and still closes what
        # it holds; only a cancellation aimed at the stop itself is re-raised.
        asyncio.get_running_loop().call_soon(task.cancel)
        await c.async_stop()

        assert task.cancelled()
        assert link.closed == 1, "the session link was left open"
        assert c._initial_client is None, "the handed-off link was left open"

    asyncio.run(run())


def test_an_unadopted_pairing_link_is_closed_too() -> None:
    async def run() -> None:
        handoff = _Link()
        c = _Coord(initial=handoff)
        # No session task at all: this is the window between setup handing the
        # pairing connection over and the first attempt adopting it, which is
        # exactly where a reload used to drop it still connected.
        await c.async_stop()
        assert handoff.closed == 1, "the handed-off link was left open"
        assert c._initial_client is None, "a closed link is still referenced"

    asyncio.run(run())


def _hass(*, unload_ok: bool) -> SimpleNamespace:
    return SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_unload_platforms=_returning(unload_ok),
            async_forward_entry_setups=_returning(None),
        ),
    )


def _returning(value):
    async def _call(*_a, **_kw):
        return value

    return _call


class _Stopped:
    """A coordinator that only records that it was stopped."""

    def __init__(self) -> None:
        self.stops = 0

    async def async_stop(self) -> None:
        self.stops += 1


def test_a_platform_that_refuses_to_unload_still_stops_the_session() -> None:
    async def run() -> None:
        coordinator = _Stopped()
        entry = SimpleNamespace(runtime_data=coordinator)
        ok = await ENTRY.async_unload_entry(_hass(unload_ok=False), entry)
        assert ok is False, "a failed platform unload was reported as success"
        assert coordinator.stops == 1, (
            "the session was left running behind an entry nobody reads"
        )

    asyncio.run(run())


def test_a_setup_that_fails_late_stops_the_session_it_started() -> None:
    async def run() -> None:
        coordinator = _Stopped()

        async def _boom(*_a, **_kw):
            raise RuntimeError("the device registry said no")

        started: list[str] = []

        class _Coordinator:
            def __init__(self, *_a, **_kw) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                pass

            async def async_start(self) -> None:
                started.append("session")

            async def async_stop(self) -> None:
                coordinator.stops += 1

        original = (
            ENTRY.TrumaCoordinator,
            ENTRY._async_finish_setup,
            ENTRY._async_register_card,
        )
        ENTRY.TrumaCoordinator = _Coordinator
        ENTRY._async_finish_setup = _boom
        ENTRY._async_register_card = _returning(None)
        try:
            entry = SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"},
                                    runtime_data=None)
            raised = None
            try:
                await ENTRY.async_setup_entry(_hass(unload_ok=True), entry)
            except RuntimeError as exc:
                raised = exc
            assert raised is not None, "the setup failure was swallowed"
            assert started == ["session"]
            assert coordinator.stops == 1, (
                "setup failed with its session still running: Home Assistant "
                "never unloads an entry whose setup raised"
            )
        finally:
            (
                ENTRY.TrumaCoordinator,
                ENTRY._async_finish_setup,
                ENTRY._async_register_card,
            ) = original

    asyncio.run(run())


if __name__ == "__main__":
    stubs.run_tests(globals(), "entry teardown")
