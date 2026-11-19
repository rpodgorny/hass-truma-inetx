#!/usr/bin/env python3
"""A session that fails on a live link is restarted on it, not closed.

Why (measured on the van, 2026-10 and 2026-11): once a link to the panel is
closed, the panel refuses every new connection until it is power-cycled. The
integration used to close the link for reasons that had nothing to do with the
link -- a 90 s data stall, a startup that did not finish, a send whose
acknowledgement went missing -- and so turned a protocol hiccup into a panel
that needed someone to switch it off and on. The ESPHome component written for
this panel avoids exactly that by never closing a working link itself and
restarting its session over the radio link instead; this does the same.

What it pins, all against the real coordinator code:

1. a stall on a live link restarts the session on that link, twice at most,
   and only then hands the session back to be closed as before,
2. a restart that then runs healthily for a while is not held against the next
   failure,
3. a link that went away is never "restarted" -- it is reconnected as before,
4. a send that went unacknowledged on a live link restarts the session too,
5. a startup that fails on a live link is retried in place, then given up,
6. poll mode never restarts in place (the next poll is the restart),
7. the restart needs the link: one that drops meanwhile hands back at once,
8. a write is refused while a restarted session has not registered yet,
9. a link closed from outside is said to be, and our own close is not,
10. closing a live link because a session failed is a warning that says why.

Run: ``python3 tests/test_in_link_restart.py``
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

stubs.install_homeassistant()
stubs.stub_transport()
stubs.load_truma("const")
# Nothing here builds or parses a frame -- startup and the transport are both
# doubles -- so the protocol module, and the cbor2 it needs, are stubbed. That
# keeps this file in CI's first stage, which runs with no library installed.
_unused = lambda *_a, **_k: b""  # noqa: E731
stubs.mod(
    "truma_pkg.truma.protocol",
    build_identity_frames=lambda *_a, **_k: [],
    build_register_frame=_unused,
    build_subscribe_frame=_unused,
    build_v3_frame=_unused,
    build_write_frame=_unused,
    parse_v3_frame=lambda *_a, **_k: None,
)
BUS = stubs.load("bus")
stubs.load("const")
SESSION = stubs.load("session")
COORD = stubs.load("coordinator")

HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError


class _Clock:
    """Virtual loop clock; the sleeps under test advance it."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


class _FastForward:
    """``asyncio`` stand-in whose ``sleep`` advances the clock instead."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *_a, **_kw):
        self._clock.now += delay
        await asyncio.sleep(0)


def _run(coro, clock: _Clock):
    shim = _FastForward(clock)
    real = COORD.asyncio
    COORD.asyncio = shim
    try:
        return asyncio.run(coro)
    finally:
        COORD.asyncio = real


class _Client:
    """The transport, reduced to the two questions this is about.

    ``link_up`` is the BLE link; ``connected`` is whether the session on it can
    still send. They differ exactly when a send has gone unanswered, which is
    the state ``invalidate`` puts it in.
    """

    transport = "proxy"

    def __init__(self, clock: _Clock, *, drop_at: float | None = None) -> None:
        self._clock = clock
        self._drop_at = drop_at
        self.invalid = False
        self.resets = 0
        self.closed = 0
        self.assigned_addr = 0x0501
        self.registered = True

    @property
    def link_up(self) -> bool:
        return self._drop_at is None or self._clock.now < self._drop_at

    @property
    def connected(self) -> bool:
        return self.link_up and not self.invalid

    async def reset_session(self) -> bool:
        if not self.link_up:
            return False
        self.resets += 1
        self.invalid = False
        self.registered = False
        return True

    async def send(self, _frame, *, probe: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self.closed += 1


class _Coord:
    """Carries only what the session-holding methods touch."""

    unique_id = "Truma iNetX-FFB4D1"
    config_entry = types.SimpleNamespace(entry_id="entry-1")

    def __init__(self, clock: _Clock, *, poll_interval: int = 0) -> None:
        self.poll_interval = poll_interval
        self.hass = types.SimpleNamespace(loop=types.SimpleNamespace(time=clock.time))
        self._clock = clock
        self._bus = BUS.Bus()
        self._last_frame = 0.0
        self._stop = False
        self._last_kind = None
        self._session_ok = False
        self._refused_sessions = 0
        self._silent_sessions = 0
        self._writes_pending = 0
        self._connected_event = asyncio.Event()
        self._end_reason = None
        self._closing_link = False
        self._client = None
        self.startups = 0
        # Scripted by each test: whether the next startup fails, whether the
        # panel is talking at a given moment, and when to stop.
        self.startup_fails = lambda n: False
        self.talking = lambda now: False
        self.stop_at: float | None = None
        self.on_tick = lambda: None

    def async_set_updated_data(self, _data) -> None:
        pass

    def async_sync_device_names(self) -> None:
        pass

    def _async_clear_refused(self) -> None:
        self._refused_sessions = 0

    def _async_clear_silent(self) -> None:
        self._silent_sessions = 0

    async def _run_startup(self, client) -> None:
        self.startups += 1
        if self.startup_fails(self.startups):
            raise HomeAssistantError(
                f"Truma {self.unique_id}: the panel assigned us no address within 20s"
            )
        client.registered = True

    async def _request_measurements(self, _client) -> None:
        """Stands in for the once-a-minute ask, and for the panel talking."""

    _finish_startup = COORD.TrumaCoordinator._finish_startup
    _run_session = COORD.TrumaCoordinator._run_session
    _may_restart_in_link = COORD.TrumaCoordinator._may_restart_in_link
    _restart_in_link = COORD.TrumaCoordinator._restart_in_link
    _disconnect_client = COORD.TrumaCoordinator._disconnect_client
    _on_link_dropped = COORD.TrumaCoordinator._on_link_dropped
    _client_for_write = COORD.TrumaCoordinator._client_for_write


def _drive(coord: _Coord, client: _Client):
    """Run _finish_startup with the panel talking as the test scripts it.

    The hold loop ticks once a second; a frame arriving is what feeds its
    stall watchdog, so 'talking' is modelled as a frame on every tick.
    """
    clock = coord._clock

    async def feeder() -> None:
        while True:
            await asyncio.sleep(0)
            if coord.talking(clock.now):
                coord._last_frame = clock.now
            if coord.stop_at is not None and clock.now >= coord.stop_at:
                coord._stop = True
            coord.on_tick()

    async def main():
        task = asyncio.ensure_future(feeder())
        try:
            return await coord._finish_startup(client)
        finally:
            task.cancel()

    return _run(main(), clock)


def test_a_stall_on_a_live_link_restarts_the_session_on_it() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)

    assert _drive(coord, client) is True

    assert client.resets == COORD._IN_LINK_RESTARTS_MAX, (
        f"restarted {client.resets} time(s) on a link that was up throughout"
    )
    assert coord.startups == COORD._IN_LINK_RESTARTS_MAX + 1
    assert client.closed == 0, "the session closed the link itself"
    # ...and then handed back, with the reason the close will be logged under.
    assert coord._end_reason == f"no data for {COORD._DATA_STALL_TIMEOUT}s"


def test_a_restart_that_worked_is_not_held_against_the_next_failure() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    # Two quick stalls use both restarts; then the panel talks for long enough
    # to prove the second one worked and goes quiet for good. That later
    # failure is a new one and gets the full allowance again before the
    # session is handed back.
    coord.talking = lambda now: 200 <= now < 1100

    _drive(coord, client)

    assert client.resets == 2 * COORD._IN_LINK_RESTARTS_MAX, (
        f"{client.resets} restart(s): a session that ran healthily for 15 "
        "minutes was judged by the two restarts before it"
    )


def test_a_link_that_went_away_is_not_restarted() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock, drop_at=50)
    coord.talking = lambda now: True

    assert _drive(coord, client) is True

    assert client.resets == 0, "restarted a session on a link that is gone"
    assert coord._end_reason == "the link went away"


def test_an_unacknowledged_send_on_a_live_link_restarts_the_session() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    coord.talking = lambda now: True
    coord.stop_at = 200

    def tick() -> None:
        # The transport gives the session up once, half a minute in.
        if 30 <= clock.now < 31 and client.resets == 0:
            client.invalid = True

    coord.on_tick = tick

    _drive(coord, client)

    assert client.resets == 1, f"{client.resets} restart(s) for one lost ack"
    assert client.closed == 0


def test_a_startup_failure_on_a_live_link_is_retried_in_place_then_given_up() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    coord.startup_fails = lambda n: True

    try:
        _drive(coord, client)
    except HomeAssistantError:
        pass
    else:
        raise AssertionError("a startup that never succeeds was not given up")

    assert client.resets == COORD._IN_LINK_RESTARTS_MAX
    assert coord.startups == COORD._IN_LINK_RESTARTS_MAX + 1


def test_a_startup_that_recovers_in_place_carries_on() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    coord.startup_fails = lambda n: n == 1
    coord.talking = lambda now: True
    coord.stop_at = 300

    assert _drive(coord, client) is True

    assert client.resets == 1
    assert coord._bus.connected, "the recovered session was never reported up"


def test_poll_mode_never_restarts_in_place() -> None:
    clock = _Clock()
    coord = _Coord(clock, poll_interval=300)
    client = _Client(clock)
    coord.startup_fails = lambda n: True

    try:
        _drive(coord, client)
    except HomeAssistantError:
        pass
    assert client.resets == 0, "poll mode restarted a session in place"


def test_the_restart_needs_the_link() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    # Up for the stall, gone by the end of the settle wait.
    client = _Client(clock, drop_at=COORD._DATA_STALL_TIMEOUT + 2)

    try:
        _drive(coord, client)
    except HomeAssistantError as exc:
        assert "went away" in str(exc)
    else:
        raise AssertionError("a restart went ahead on a link that had gone")
    assert client.resets == 0


def test_a_write_waits_for_the_restarted_session_to_register() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    client.registered = False
    coord._client = client

    try:
        _run(coord._client_for_write(), clock)
    except HomeAssistantError:
        pass
    else:
        raise AssertionError("a write was let through before registration")

    client.registered = True
    assert _run(coord._client_for_write(), clock) is client


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture() -> _Records:
    handler = _Records()
    COORD.LOGGER.addHandler(handler)
    COORD.LOGGER.setLevel(logging.DEBUG)
    return handler


def test_a_link_closed_from_outside_is_said_to_be() -> None:
    clock = _Clock()
    clock.now = 500
    coord = _Coord(clock)
    coord._client = _Client(clock)
    coord._last_frame = 497
    log = _capture()
    try:
        coord._on_link_dropped(object())
    finally:
        COORD.LOGGER.removeHandler(log)

    warnings = [r for r in log.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in log.records]
    message = warnings[0].getMessage()
    assert "closed from outside" in message and "ESPHome proxy" in message
    assert "3s ago" in message, message
    assert coord._end_reason == "the link was closed from outside"


def test_our_own_close_is_not_reported_as_one_from_outside() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    client = _Client(clock)
    coord._client = client

    async def disconnect() -> None:
        # bleak calls the disconnect callback from inside our own close.
        coord._on_link_dropped(object())

    client.disconnect = disconnect
    log = _capture()
    try:
        _run(coord._disconnect_client("the poll is complete"), clock)
    finally:
        COORD.LOGGER.removeHandler(log)

    assert not any(
        "closed from outside" in r.getMessage() for r in log.records
    ), "our own close was reported as one from outside"
    assert coord._closing_link is False


def test_closing_a_live_link_after_a_failure_is_a_warning_that_says_why() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    coord._client = _Client(clock)
    log = _capture()
    try:
        _run(coord._disconnect_client("no data for 90s"), clock)
    finally:
        COORD.LOGGER.removeHandler(log)

    closing = [r for r in log.records if "closing the BLE link" in r.getMessage()]
    assert len(closing) == 1
    assert closing[0].levelno == logging.WARNING
    assert "no data for 90s" in closing[0].getMessage()


def test_closing_for_a_stop_is_only_info() -> None:
    clock = _Clock()
    coord = _Coord(clock)
    coord._client = _Client(clock)
    coord._stop = True
    log = _capture()
    try:
        _run(coord._disconnect_client(), clock)
    finally:
        COORD.LOGGER.removeHandler(log)

    closing = [r for r in log.records if "closing the BLE link" in r.getMessage()]
    assert len(closing) == 1 and closing[0].levelno == logging.INFO
    assert "stopping" in closing[0].getMessage()


if __name__ == "__main__":
    stubs.run_tests(globals(), "in-link restart")
