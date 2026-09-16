"""BLE transport for Truma iNet X over Home Assistant's Bluetooth stack.

This is the bleak/HA-bluetooth port of the project's original dbus-fast
transport. The framing/CBOR protocol (``truma/protocol.py``) is reused
unchanged; only the connection + GATT I/O layer differs.

Transport FSM (per ``send``):
  1. Write ``[0x01, len_lo, len_hi]`` (InitDataTransfer) to CMD (with response).
  2. Wait for a Ready notification (0x81) on CMD.
  3. Write the packet to DATA_W (without response).
  4. Wait for a DataAck notification (0xF0) on CMD.

Incoming messages go the other way: the panel announces one with
``[0x83, len_lo, len_hi]`` on CMD (answered with 0x0300), then pushes it on
DATA_R in ATT-sized fragments. The fragments are accumulated to the announced
length, ACKed once (0xF001), parsed into a V3 frame and dispatched to the
registered callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .truma.const import (
    CHAR_CMD,
    CHAR_DATA_R,
    CHAR_DATA_W,
    DEV_APP_DEFAULT,
    TRANSPORT_ACK,
    TRANSPORT_CONFIRM,
    TRANSPORT_MSG_ACK,
    TRANSPORT_INIT,
    TRANSPORT_READY,
)
from .truma.protocol import parse_v3_frame

_LOGGER = logging.getLogger(__name__)


# BlueZ refuses a connect while one is already in flight for the same device --
# its own background reconnect of a bonded device, or a leftover of ours. That
# attempt usually finishes within seconds and leaves a link we can simply attach
# to, so waiting a moment beats failing the session and backing off past the
# window (measured on the van: BlueZ connects, resolves GATT, and holds the
# link, while our next attempt only came 45 s later and collided again).
# Long enough that the previous attempt has finished inside BlueZ before the
# next one starts. Retrying faster than BlueZ can connect (~20 s here) just
# collides with our own outstanding call and every attempt is refused with
# "Operation already in progress" while the device sits there connected.

# ...and much longer than that once we have just cleared a dial. Clearing drops
# BlueZ's Connected but leaves the kernel link, which bt-ghostbuster only takes
# down at its 2-minute mark; BlueZ reconnects a second or two later. Dialling
# inside that window is how the van oscillated at 15:36-15:38 -- dial, BlueZ
# arrives, clear, dial again -- recreating the pending connect every time.




def client_is_proxy(client: object) -> bool:
    """Whether this client talks through an ESPHome proxy rather than BlueZ.

    Proxy clients come from ``bleak_esphome``; a local adapter gives BlueZ's
    ``bleak.backends.bluezdbus`` client. The two need opposite handling where
    encryption is concerned -- see :meth:`TrumaBle._subscribe` and
    ``pairing.ensure_bonded`` -- and this is the honest way to ask: it reads
    the backend Home Assistant actually chose for this link, after the fact.
    Which scanner heard the panel, or which transports could reach it, predicts
    nothing, because HA re-scores the paths at every connect.
    """
    backend = getattr(client, "_backend", client)
    return "esphome" in type(backend).__module__

# establish_connection does the retrying now. Three attempts is plenty when a
# connect takes ~4 s; it used to take 40+ s and needed a hand-rolled loop.
_CONNECT_ATTEMPTS = 3

_READY_TIMEOUT = 3.0
_ACK_TIMEOUT = 3.0

# A GATT write on a live link completes in milliseconds. This is not a latency
# budget, it is the line between "slow" and "never": a Write Request the panel
# never answers leaves BlueZ's D-Bus call outstanding with no timeout of its
# own, and nothing else in the session is watching yet, because the stall
# watchdog only starts once startup has finished.
#
# Measured on the van 2026-09-15 22:45:33: the link came up, the panel sent two
# frames and went quiet, and the first write of the session hung. It was still
# hanging four minutes later, when the link was forced down from outside --
# whereupon the session reported the link ending "during registration" within
# a second and reconnected cleanly. Left alone it would have hung for as long
# as Home Assistant ran, "connected" the whole time. The panel meanwhile
# re-announced an incoming message every five seconds, so the link was neither
# gone nor idle; it simply took nothing.
_WRITE_TIMEOUT = 5.0

# The same, on the way out. BlueZ's Device1.Disconnect can return without
# reaching the radio -- measured on the van, a disconnect through it left the
# controller's connection up, while btmgmt dropped it at once -- and it can
# also not return at all. This one is awaited from the send lock's cleanup, so
# a hang there holds the lock as well as the session.
_DISCONNECT_TIMEOUT = 5.0


async def close_link(client: BleakClientWithServiceCache, label: str) -> None:
    """Close a BLE link and never raise, whatever the stack does with it.

    The bound on the wait is the point: an unbounded disconnect is awaited by
    whoever is tearing a session down -- the send lock's cleanup, or Home
    Assistant unloading the config entry -- and a hang there holds them, not
    just the link. See _DISCONNECT_TIMEOUT.

    ``label`` only names the link in the log; this knows nothing about who owns
    it, which is why the coordinator can close a connection the config flow
    made with it too.
    """
    try:
        await asyncio.wait_for(client.disconnect(), _DISCONNECT_TIMEOUT)
    except TimeoutError:
        # Nothing here can make the link go away if BlueZ will not; what this
        # does is stop the wait from outliving the session that started it.
        _LOGGER.warning(
            "Truma %s: the BLE disconnect did not return within %ss; "
            "letting the link go and carrying on",
            label,
            _DISCONNECT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - teardown must not raise
        _LOGGER.debug("Truma %s BLE disconnect error: %s", label, exc)


async def device_from_bluez(address: str) -> BLEDevice | None:
    """Build a BLEDevice from BlueZ's own object, with no advert involved.

    A bonded panel that something already holds a link to does not advertise --
    it has a central. HA's discovery cache therefore runs empty exactly when the
    link is healthiest, and the coordinator has nothing to hand to bleak, so it
    gives up with "not currently advertising" while BlueZ is sitting on a fully
    resolved connection (measured on the van 2026-08-19 13:41). BlueZ still has
    the device object, and its path plus properties is all bleak's BlueZ backend
    needs -- it is exactly what bleak's own scanner puts in ``details``.
    """
    try:
        from bleak.backends.bluezdbus import defs
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        want = address.upper()
        # Same private map bleak's own is_connected()/is_paired() read.
        for path, interfaces in manager._properties.items():  # noqa: SLF001
            props = interfaces.get(defs.DEVICE_INTERFACE)
            if props and props.get("Address", "").upper() == want:
                return BLEDevice(
                    want,
                    props.get("Alias") or props.get("Name"),
                    {"path": path, "props": props},
                )
    except Exception as exc:  # noqa: BLE001 - a fallback may simply not apply
        _LOGGER.debug("Truma: cannot build a device from BlueZ: %s", exc)
    return None


class TrumaBleClient:
    """Manage the BLE connection and transport FSM to a Truma iNet X panel."""

    def __init__(self, identity: dict) -> None:
        """Initialize with the app identity (muid/uuid/username)."""
        self._identity = identity
        self._client: BleakClientWithServiceCache | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._data_callbacks: list[Callable[[dict], None]] = []
        self._send_lock = asyncio.Lock()
        self._transport_event: asyncio.Event | None = None
        self._transport_ack: bytes | None = None
        # The opcodes the transfer in flight is waiting for, if any. Nothing
        # else on CMD may satisfy that wait -- see :meth:`_handle_cmd`.
        self._transport_expected: tuple[int, ...] = ()
        self._transport_invalidated = False
        # Reassembly of the incoming message the panel announced, if any.
        self._receive_size: int | None = None
        self._receive_buffer = bytearray()
        self.assigned_addr = DEV_APP_DEFAULT

    def on_data(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for decoded V3 frames."""
        self._data_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        """Whether the BLE link is up *and* still usable.

        An invalidated transport reports disconnected even in the moment
        before the link actually goes away, so that nothing queues another
        packet onto a stream whose acks can no longer be attributed.
        """
        return (
            not self._transport_invalidated
            and self._client is not None
            and self._client.is_connected
        )

    @property
    def transport(self) -> str | None:
        """Which adapter this link runs over: proxy, local, or None if down.

        Which path Home Assistant picked is not ours to choose and nothing in
        the session reads it (see bt.py). It is recorded because issue #13
        turns on whether the bond and the session live on the same adapter --
        a host with both a local adapter and a proxy in earshot can bond over
        one and then run every session over the other -- and a download that
        does not say which one carried the session cannot answer that.
        """
        if self._client is None:
            return None
        return "proxy" if client_is_proxy(self._client) else "local"

    async def connect(
        self,
        ble_device: BLEDevice,
        disconnected_callback: Callable[[BleakClientWithServiceCache], None]
        | None = None,
    ) -> None:
        """Establish the connection (via HA's stack) and subscribe.

        Plain ``establish_connection``, which is transport-agnostic: it works
        the same over BlueZ and over an ESPHome proxy, so nothing here needs to
        know which one it got.

        This used to be ~250 lines of BlueZ-specific machinery -- dialling over
        the system bus, watching Connected/ServicesResolved, clearing dials with
        Device1.Disconnect. All of it existed to work around one kernel bug: an
        RPA dialled as its identity address, which could never be answered and
        burned 20 s per attempt. Kernel v8 fixed that at the source; connects
        went from 40+ s and two dials to about 4 s, and the workarounds had
        nothing left to work around. See DUCATO_STATE.md, 2026-08-20.
        """
        self._loop = asyncio.get_running_loop()
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or ble_device.address,
            disconnected_callback=disconnected_callback,
            max_attempts=_CONNECT_ATTEMPTS,
            use_services_cache=True,
        )
        await self._subscribe()
        self._transport_invalidated = False

    async def adopt(self, client: BleakClientWithServiceCache) -> None:
        """Take over an already-connected client (handed off from pairing).

        The config-flow bond leaves a live, encrypted connection; reusing it for
        the session avoids the disconnect-then-reconnect that wedges the panel's
        just-bonded RPA (the "needs a power-cycle" bug). The caller must have
        verified the client is still connected. Subscribes on the live link.
        """
        self._loop = asyncio.get_running_loop()
        self._client = client
        await self._subscribe()
        self._transport_invalidated = False

    async def _subscribe(self) -> None:
        """Establish encryption, then enable notifications.

        The panel's characteristics require an encrypted link. The proxy only
        encrypts on first *protected access*, so a bare CCCD write races ahead
        of encryption and fails (status 5 = insufficient auth when unbonded,
        status 15 = insufficient encryption on a bonded reconnect) — and the
        proxy tears the connection down on that failed write. So pair/encrypt
        FIRST (when already bonded this just re-establishes encryption), then
        subscribe, retrying briefly to absorb the encryption-setup delay.

        That is true for an ESPHome proxy only. On a LOCAL adapter the stack is
        BlueZ, where Device.Pair() on an already-bonded peer raises
        AuthenticationFailed and takes the connection down with it — the next
        subscribe then fails with NotConnected. BlueZ encrypts by itself on the
        first protected access, so the correct move locally is to skip pair()
        entirely and go straight to subscribing.
        """
        assert self._client is not None
        last_exc: Exception | None = None
        pair_first = client_is_proxy(self._client)
        _LOGGER.debug(
            # The backend class names the path Home Assistant actually chose,
            # which is not the one the resolver's device came from -- HA scores
            # the paths again at connect time. It is the only record of which
            # adapter carried a session, and the first thing a bug report needs.
            "Truma subscribe: transport=%s (%s), pair() %s",
            "proxy" if pair_first else "local",
            type(getattr(self._client, "_backend", self._client)).__name__,
            "first" if pair_first else "skipped",
        )
        for attempt in range(3):
            if pair_first:
                try:
                    await self._client.pair()
                except Exception as exc:  # noqa: BLE001 - some backends bond out-of-band
                    _LOGGER.debug("Truma pair()/encrypt attempt %d: %s", attempt, exc)
            try:
                await self._start_notifications()
                _LOGGER.debug("Truma BLE connected and subscribed")
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _LOGGER.debug("Truma subscribe attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(1.5)
        if last_exc is not None:
            raise last_exc

    async def _start_notifications(self) -> None:
        """Subscribe to CMD (transport acks) and DATA_R (data) notifications."""
        assert self._client is not None
        await self._client.start_notify(CHAR_CMD, self._notify_cmd)
        await self._client.start_notify(CHAR_DATA_R, self._notify_data)

    async def disconnect(self) -> None:
        """Disconnect the BLE link."""
        # A half-received message belongs to the session that is ending; the
        # next one must not be assembled onto its tail.
        self._receive_size = None
        self._receive_buffer.clear()
        client = self._client
        self._client = None
        if client is not None:
            await close_link(client, "session")

    # -- notifications ---------------------------------------------------

    def _notify_cmd(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_CMD, bytes(data))

    def _notify_data(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_DATA_R, bytes(data))

    def _handle_notification(self, char_uuid: str, data: bytes) -> None:
        # The whole transport is inferred from these two channels, and a bug
        # report has nothing else to go on. Seven bytes is the frame header.
        _LOGGER.debug(
            "Truma RX %s, %d bytes: %s",
            "CMD" if char_uuid == CHAR_CMD else "DATA",
            len(data),
            data[:7].hex(),
        )
        if char_uuid == CHAR_CMD:
            self._handle_cmd(data)
        else:
            self._handle_data(data)

    def _handle_cmd(self, data: bytes) -> None:
        """Handle a transport-control notification.

        0x83 is *not* an acknowledgement of anything we sent. It announces an
        INCOMING message and its little-endian size, and is answered with
        0x0300. Counting it as an ack is how ``send`` reported success on a
        frame the panel never took: an announcement that happened to land
        mid-transfer was accepted in place of the DataAck.

        Everything else is only interesting while a transfer is waiting for
        one specific opcode. Ready (0x81) and DataAck (0xF0) carry no transfer
        identity, so an unexpected one has to be dropped rather than used to
        satisfy whatever wait happens to be open -- a second Ready arriving
        after the payload write used to be taken for the DataAck, failing a
        write that had in fact succeeded.

        Measured on the van 2026-09-14 01:33:38, a heater-routed write::

            Truma write AirCirculation.FanLevel = 2 -> 0x0201
            RX CMD, 2 bytes: 8100      Ready
            RX CMD, 2 bytes: f001      DataAck -- this is the acknowledgement
            RX CMD, 3 bytes: 834c00    announce, 0x004c = 76 bytes
            RX DATA, 76 bytes: ...     the heater's reply, exactly 76

        which is where the old "heater-routed writes reply MsgAck, panel
        writes DataAck" came from: the reply's announcement lands ~15 ms after
        the real ack and was being counted as one. The same session also
        carried a bare ``f004`` and a one-byte ``79`` outside any transfer --
        neither is an ack, and both used to satisfy whatever wait was open.
        """
        if len(data) >= 3 and data[0] == TRANSPORT_MSG_ACK:
            self._receive_size = int.from_bytes(data[1:3], "little")
            self._receive_buffer.clear()
            self._fire_write(CHAR_CMD, bytes([TRANSPORT_CONFIRM, 0x00]))
            return
        if len(data) > 4:
            return
        if data and data[0] in self._transport_expected:
            self._transport_ack = data
            if self._transport_event is not None:
                self._transport_event.set()

    def _handle_data(self, data: bytes) -> None:
        """Reassemble an incoming message, then ACK it once and dispatch it.

        DATA_R is fragmented at the negotiated ATT payload size, and a
        fragment is not a frame: ``parse_v3_frame`` rejects only the first 16
        bytes being absent, so a truncated head parsed happily into a frame
        with no CBOR while the rest of the message was dropped on the floor.
        The 0x83 announcement carries the full length, so accumulate to it and
        acknowledge once, at the end.

        Without a preceding announcement there is nothing to accumulate to;
        take the notification for a whole frame, which is what it was before
        any of this.

        Measured on the van 2026-09-14: a 256-byte message (``830001``)
        arrived as 248 + 8 bytes, so 248 is the usable ATT payload on that
        link and anything above it fragments. Of 116 announcements in one
        session the length matched the payload exactly every time. An
        announcement is also sometimes repeated before its data arrives,
        which is why each one resets the buffer rather than appending.
        """
        if self._receive_size is not None:
            self._receive_buffer.extend(data)
            if len(self._receive_buffer) < self._receive_size:
                return
            if len(self._receive_buffer) > self._receive_size:
                # The stream is out of step with the announcement; assembling
                # further would only produce garbage frames.
                _LOGGER.warning(
                    "Truma: incoming message overran its announced %d bytes",
                    self._receive_size,
                )
                self._receive_size = None
                self._receive_buffer.clear()
                return
            data = bytes(self._receive_buffer)
            self._receive_size = None
            self._receive_buffer.clear()

        self._fire_write(CHAR_CMD, bytes([TRANSPORT_ACK, 0x01]))
        frame = parse_v3_frame(data)
        if frame is not None:
            for callback in self._data_callbacks:
                try:
                    callback(frame)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Truma data callback error")

    def _fire_write(self, char_uuid: str, data: bytes) -> None:
        """Schedule a fire-and-forget GATT write from a notification handler."""
        if self._loop is not None:
            self._loop.create_task(self._fire_and_forget(char_uuid, data))

    async def _fire_and_forget(self, char_uuid: str, data: bytes) -> None:
        """Run a scheduled write with nobody waiting on the result.

        These are the transport's own replies -- the confirm for a message the
        panel announced, the ack for one it delivered -- and no caller awaits
        the task, so an exception would otherwise surface as Home Assistant's
        "Task exception was never retrieved" and nothing more. A write that
        times out has already given the session up (see :meth:`_write`), which
        is the part that matters; this only keeps the log honest about it.
        """
        try:
            await self._write(char_uuid, data)
        except Exception as exc:  # noqa: BLE001 - nothing awaits this
            _LOGGER.debug("Truma transport reply to %s failed: %s", char_uuid, exc)

    # -- sending ---------------------------------------------------------

    async def _write(self, char_uuid: str, data: bytes) -> None:
        """Write to a characteristic, giving the session up if it hangs."""
        if self._client is None:
            return
        # CMD uses Write Request (with response); DATA_W uses Write Command.
        response = char_uuid == CHAR_CMD
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(char_uuid, data, response=response),
                _WRITE_TIMEOUT,
            )
        except TimeoutError:
            # See _WRITE_TIMEOUT. A link that does not take this write will
            # not take the next one either, and every later wait on it would
            # hang the same way -- so end the session here rather than one
            # timeout at a time. `connected` reads False from this instant,
            # which is what the startup and hold loops watch.
            self._transport_invalidated = True
            _LOGGER.warning(
                "Truma: a %d-byte write to %s went unanswered for %ss; the "
                "link is up and takes nothing, so the session is being given "
                "up and retried",
                len(data),
                "CMD" if char_uuid == CHAR_CMD else "DATA",
                _WRITE_TIMEOUT,
            )
            raise

    async def send(self, packet: bytes, *, probe: bool = False) -> bool:
        """Send a V3 packet through the transport FSM. Returns True on DataAck.

        An unanswered send normally ends the session -- see ``_send_locked``.
        ``probe`` says the caller is addressing something that may not be
        there and that silence is one of the answers it expects, so the
        session is left alone. Only parameter discovery has that shape: it
        sweeps a seed of bus addresses, most of which are empty on any given
        vehicle. It is also the one caller whose return value decides nothing
        beyond a debug counter, and it leaves a 3 s settle at the end of the
        sweep for any late reply to be discarded in.
        """
        async with self._send_lock:
            return await self._send_locked(packet, probe=probe)

    async def _send_locked(self, packet: bytes, *, probe: bool = False) -> bool:
        if self._transport_invalidated:
            return False
        success = False
        try:
            self._transport_event = asyncio.Event()
            self._transport_ack = None
            self._transport_expected = (TRANSPORT_READY,)

            # 1. InitDataTransfer announce.
            announce = bytes(
                [TRANSPORT_INIT, len(packet) & 0xFF, (len(packet) >> 8) & 0xFF]
            )
            await self._write(CHAR_CMD, announce)

            # 2. Wait for Ready. Without it the panel is not listening on
            #    DATA_W, and writing the payload anyway only desynchronises the
            #    stream: the reply to *that* would arrive against the next
            #    transfer.
            try:
                await asyncio.wait_for(self._transport_event.wait(), _READY_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for Ready")
                return False
            self._transport_event.clear()
            self._transport_ack = None
            self._transport_expected = (TRANSPORT_ACK,)

            # 3. Send payload on DATA_W.
            await self._write(CHAR_DATA_W, packet)

            # 4. Wait for DataAck. Only a positive 0xF001 is success: 0xF000
            #    is the panel refusing the frame, and 0x83 is not an answer to
            #    this transfer at all (see :meth:`_handle_cmd`).
            try:
                await asyncio.wait_for(self._transport_event.wait(), _ACK_TIMEOUT)
                if self._transport_ack == bytes([TRANSPORT_ACK, 0x01]):
                    success = True
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for DataAck")

            # 5. Let any async follow-up settle.
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            success = False
            raise
        except Exception as exc:  # noqa: BLE001
            success = False
            _LOGGER.debug("Truma transport error: %s", exc)
        finally:
            self._transport_event = None
            self._transport_ack = None
            self._transport_expected = ()
            if not success and not probe:
                # Ready and DataAck carry no transfer identity, so a late reply
                # to *this* transfer cannot be told apart from the reply to the
                # next one. Any unsuccessful send therefore leaves the stream
                # ambiguous, and the only safe answer is to end the session
                # before the send lock is released -- otherwise the packet
                # behind us is acknowledged by an ack that was never its own.
                # The coordinator reconnects; a dropped write is cheaper than a
                # write that reports success into thin air.
                self._transport_invalidated = True
                self.assigned_addr = DEV_APP_DEFAULT
                await self.disconnect()
        return success
