#!/usr/bin/env python3
"""Offline checks that the transport FSM attributes every reply correctly.

Extracted from tomac01's PR #21, which found these against a Combi D6 E.

Ready (0x81) and DataAck (0xF0) say nothing about *which* transfer they answer,
and 0x83 answers none of them -- it announces an incoming message. The old
handler set the transfer's event on any CMD notification and accepted 0x83 as
an ack, so a write could report success on a frame the panel never took, fail
on one it did, or hand a stale ack to the packet behind it. On DATA_R it parsed
every fragment as though it were a whole frame.

Run: ``python3 tests/test_transport_ack_order.py``
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

# Reuse the stub loader next door: it imports ble.py with bleak and the truma
# package faked out, which is the only way to exercise the transport offline.
_spec = importlib.util.spec_from_file_location(
    "truma_transport_loader", Path(__file__).with_name("test_device_from_bluez.py")
)
assert _spec is not None and _spec.loader is not None
_loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loader)
BLE = _loader.BLE

# That loader stubs every constant to 0, which would make the opcodes
# indistinguishable. Give them the values the panel actually uses.
BLE.CHAR_CMD, BLE.CHAR_DATA_W, BLE.CHAR_DATA_R = "cmd", "data", "receive"
BLE.TRANSPORT_INIT, BLE.TRANSPORT_READY = 0x01, 0x81
BLE.TRANSPORT_ACK, BLE.TRANSPORT_MSG_ACK, BLE.TRANSPORT_CONFIRM = 0xF0, 0x83, 0x03

READY = b"\x81\x00"
ACK = b"\xf0\x01"
NACK = b"\xf0\x00"


class _Link:
    """The panel's side of the link, enough for the client to talk to."""

    is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


class _DeafLink(_Link):
    """A link that takes a write and never answers it.

    What the van looked like on 2026-09-15 at 22:45: connected by everything
    the host could see, the panel re-announcing an incoming message every five
    seconds, and the first write of the session outstanding for four minutes
    until the link was forced down from outside.
    """

    def __init__(self) -> None:
        self.writes = 0

    async def write_gatt_char(self, char, data, response=False) -> None:
        self.writes += 1
        await asyncio.Event().wait()


class _StuckLink(_Link):
    """A link whose disconnect never returns, the way BlueZ's can not."""

    async def disconnect(self) -> None:
        await asyncio.Event().wait()


async def test_late_ready_does_not_hide_the_ack() -> None:
    """A second Ready after the payload must not be read as the DataAck."""
    client = BLE.TrumaBleClient({})
    loop = asyncio.get_running_loop()

    async def write(char, data):
        if char == "cmd" and data[0] == BLE.TRANSPORT_INIT:
            client._handle_notification("cmd", READY)
        elif char == "data":
            client._handle_notification("cmd", READY)
            loop.call_later(0.02, client._handle_notification, "cmd", ACK)

    client._write = write
    assert await client.send(b"payload"), "a Ready notification hid the real DataAck"


async def test_no_payload_without_ready() -> None:
    """An unrelated ack must not stand in for the Ready that gates the write."""
    client = BLE.TrumaBleClient({})
    sent_payload = False

    async def write(char, data):
        nonlocal sent_payload
        if char == "cmd":
            client._handle_notification("cmd", ACK)
        else:
            sent_payload = True

    client._write = write
    ready_timeout = BLE._READY_TIMEOUT
    BLE._READY_TIMEOUT = 0.01
    try:
        assert not await client.send(b"payload")
        assert not sent_payload, "an unrelated ack authorised the payload write"
    finally:
        BLE._READY_TIMEOUT = ready_timeout


async def test_a_negative_ack_is_not_success() -> None:
    """0xF000 is the panel refusing the frame, not taking it."""
    client = BLE.TrumaBleClient({})
    client._client = _Link()

    async def write(char, data):
        client._handle_notification("cmd", READY if char == "cmd" else NACK)

    client._write = write
    assert not await client.send(b"payload"), "a refused frame was reported as sent"


def test_a_fragmented_message_is_delivered_once_and_whole() -> None:
    """Fragments accumulate to the announced size; nothing parses a partial."""
    client = BLE.TrumaBleClient({})
    parsed, delivered, replies = [], [], []
    message = bytes(range(60))
    client._fire_write = lambda char, data: replies.append((char, data))
    original = BLE.parse_v3_frame
    BLE.parse_v3_frame = lambda data: parsed.append(data) or {"raw": data}
    client.on_data(delivered.append)
    try:
        client._handle_notification("cmd", b"\x83\x3c\x00")
        client._handle_notification("receive", message[:30])
        assert not parsed, "a partial fragment was parsed as a complete V3 frame"
        client._handle_notification("receive", message[30:59])
        assert not delivered
        client._handle_notification("receive", message[59:])
        assert parsed == [message]
        assert delivered == [{"raw": message}]
        # 0x0300 answers the announcement; the single 0xF001 answers the
        # assembled message, not each fragment of it.
        assert replies == [("cmd", b"\x03\x00"), ("cmd", b"\xf0\x01")]
    finally:
        BLE.parse_v3_frame = original


def test_an_unannounced_notification_is_still_a_frame() -> None:
    """No announcement means no reassembly -- take the notification as-is."""
    client = BLE.TrumaBleClient({})
    delivered = []
    client._fire_write = lambda *args: None
    original = BLE.parse_v3_frame
    BLE.parse_v3_frame = lambda data: {"raw": data}
    client.on_data(delivered.append)
    try:
        client._handle_notification("receive", bytes(range(40)))
        assert delivered == [{"raw": bytes(range(40))}]
    finally:
        BLE.parse_v3_frame = original


async def test_an_incoming_announcement_is_not_an_outgoing_ack() -> None:
    """0x83 announces the panel's own message; it acknowledges nothing."""
    client = BLE.TrumaBleClient({})
    client._fire_write = lambda *args: None

    async def write(char, data):
        if char == "cmd":
            client._handle_notification("cmd", READY)
        else:
            client._handle_notification("cmd", b"\x83\x3c\x00")

    client._write = write
    ack_timeout = BLE._ACK_TIMEOUT
    BLE._ACK_TIMEOUT = 0.01
    try:
        assert not await client.send(b"payload"), (
            "an incoming transfer was counted as the command's acknowledgement"
        )
    finally:
        BLE._ACK_TIMEOUT = ack_timeout


async def test_a_cancelled_transfer_cannot_acknowledge_the_next_one() -> None:
    """Cancellation mid-transfer ends the session rather than leaving it open."""
    client = BLE.TrumaBleClient({})
    payload_sent = asyncio.Event()
    sent = []
    link = _Link()
    client._client = link

    async def write(char, data):
        if char == "cmd":
            client._handle_notification("cmd", READY)
        else:
            sent.append(data)
            if data == b"A":
                payload_sent.set()
            else:
                # A's delayed ack, arriving while B is in flight.
                client._handle_notification("cmd", ACK)

    client._write = write
    first = asyncio.create_task(client.send(b"A"))
    await payload_sent.wait()
    second = asyncio.create_task(client.send(b"B"))
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert not await second, "A's delayed ack was accepted as B's success"
    assert sent == [b"A"], "a cancelled session admitted a new payload"
    assert not client.connected
    assert not link.is_connected
    assert client._transport_event is None
    assert client._transport_expected == ()


async def test_cancelling_a_queued_send_leaves_the_active_one_alone() -> None:
    """A caller that gives up while queued must not break the transfer in flight."""
    client = BLE.TrumaBleClient({})
    payload_sent = asyncio.Event()
    sent = []

    async def write(char, data):
        if char == "cmd":
            client._handle_notification("cmd", READY)
        else:
            sent.append(data)
            payload_sent.set()

    client._write = write
    first = asyncio.create_task(client.send(b"A"))
    await payload_sent.wait()
    queued = asyncio.create_task(client.send(b"B"))
    await asyncio.sleep(0)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    client._handle_notification("cmd", ACK)
    assert await first
    assert sent == [b"A"]


async def test_a_failed_transfer_cannot_acknowledge_the_next_one(failure: str) -> None:
    """However a send fails, the stream is ambiguous afterwards. End it."""
    client = BLE.TrumaBleClient({})
    started = asyncio.Event()
    payloads = []

    class Link(_Link):
        announcements = 0

        async def write_gatt_char(self, char, data, *, response):
            if char == "cmd":
                self.announcements += 1
                started.set()
                if self.announcements == 1:
                    if failure == "init_exception":
                        raise OSError("Init may have reached the panel")
                    if failure == "ready_timeout":
                        return
                client._handle_notification("cmd", READY)
            else:
                payloads.append(data)
                if data == b"A":
                    if failure == "payload_exception":
                        raise OSError("Payload may have reached the panel")
                    if failure == "negative_ack":
                        client._handle_notification("cmd", NACK)
                else:
                    # A's delayed success ack, never a confirmation of B.
                    client._handle_notification("cmd", ACK)

    link = Link()
    client._client = link
    timeouts = BLE._READY_TIMEOUT, BLE._ACK_TIMEOUT
    BLE._READY_TIMEOUT = BLE._ACK_TIMEOUT = 0.01
    try:
        first = asyncio.create_task(client.send(b"A"))
        await started.wait()
        second = asyncio.create_task(client.send(b"B"))
        assert not await first
        assert not await second, f"{failure}: a stale ack was accepted for B"
        assert b"B" not in payloads
        assert link.announcements == 1
        assert not client.connected and not link.is_connected
        assert client._transport_event is None
        assert client._transport_expected == ()
    finally:
        BLE._READY_TIMEOUT, BLE._ACK_TIMEOUT = timeouts


async def test_an_unanswered_probe_leaves_the_session_alone() -> None:
    """Parameter discovery sweeps empty addresses; silence is not a broken link."""
    client = BLE.TrumaBleClient({})
    link = _Link()
    client._client = link

    async def write(char, data):
        if char == "cmd":
            client._handle_notification("cmd", READY)

    client._write = write
    ack_timeout = BLE._ACK_TIMEOUT
    BLE._ACK_TIMEOUT = 0.01
    try:
        assert not await client.send(b"probe", probe=True)
        assert client.connected, "an unanswered probe ended the session"
        assert link.is_connected
        # ...and the session is still good for the next probe.
        assert not await client.send(b"probe", probe=True)
    finally:
        BLE._ACK_TIMEOUT = ack_timeout


async def test_a_write_that_is_never_answered_gives_the_session_up() -> None:
    """The wedge, and the whole of why _WRITE_TIMEOUT exists.

    BlueZ's Write Request has no timeout of its own, so an unanswered one
    waits for as long as the process runs -- and nothing else was watching:
    the stall watchdog only starts once startup has finished, and startup was
    what this was stuck in. Ending the session here is what turns four
    minutes of "connected" into a reconnect.
    """
    client = BLE.TrumaBleClient({})
    link = _DeafLink()
    client._client = link
    write_timeout = BLE._WRITE_TIMEOUT
    BLE._WRITE_TIMEOUT = 0.05
    try:
        assert not await client.send(b"register"), "a hung write reported success"
        assert link.writes == 1, "the payload was written to a link that took nothing"
        assert not client.connected, "the session survived a link that takes nothing"
    finally:
        BLE._WRITE_TIMEOUT = write_timeout


async def test_a_disconnect_that_never_returns_is_let_go() -> None:
    """Measured on the van: BlueZ's Device1.Disconnect can hang.

    It is awaited from the send lock's own cleanup, so waiting on it forever
    holds the lock, the session, and the adapter's connection slot with it.
    """
    client = BLE.TrumaBleClient({})
    client._client = _StuckLink()
    disconnect_timeout = BLE._DISCONNECT_TIMEOUT
    BLE._DISCONNECT_TIMEOUT = 0.05
    try:
        await asyncio.wait_for(client.disconnect(), 2)
    finally:
        BLE._DISCONNECT_TIMEOUT = disconnect_timeout
    assert client._client is None


if __name__ == "__main__":
    asyncio.run(test_late_ready_does_not_hide_the_ack())
    asyncio.run(test_no_payload_without_ready())
    asyncio.run(test_a_negative_ack_is_not_success())
    test_a_fragmented_message_is_delivered_once_and_whole()
    test_an_unannounced_notification_is_still_a_frame()
    asyncio.run(test_an_incoming_announcement_is_not_an_outgoing_ack())
    asyncio.run(test_a_cancelled_transfer_cannot_acknowledge_the_next_one())
    asyncio.run(test_cancelling_a_queued_send_leaves_the_active_one_alone())
    asyncio.run(test_an_unanswered_probe_leaves_the_session_alone())
    asyncio.run(test_a_write_that_is_never_answered_gives_the_session_up())
    asyncio.run(test_a_disconnect_that_never_returns_is_let_go())
    for _failure in (
        "ready_timeout",
        "ack_timeout",
        "init_exception",
        "payload_exception",
        "negative_ack",
    ):
        asyncio.run(test_a_failed_transfer_cannot_acknowledge_the_next_one(_failure))
    print("transport_ack_order: all checks OK")
