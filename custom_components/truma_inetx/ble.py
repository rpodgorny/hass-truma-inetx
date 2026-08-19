"""BLE transport for Truma iNet X over Home Assistant's Bluetooth stack.

This is the bleak/HA-bluetooth port of the project's original dbus-fast
transport. The framing/CBOR protocol (``truma/protocol.py``) is reused
unchanged; only the connection + GATT I/O layer differs.

Transport FSM (per ``send``):
  1. Write ``[0x01, len_lo, len_hi]`` (InitDataTransfer) to CMD (with response).
  2. Wait for a Ready notification (0x81) on CMD.
  3. Write the packet to DATA_W (without response).
  4. Wait for a DataAck notification (0xF0) on CMD.

Incoming DATA_R notifications are auto-ACKed (0xF001) and parsed into V3 frames
dispatched to registered callbacks. A short (<=4 byte) MsgAck (0x83) is
auto-confirmed with 0x0300.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
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
)
from .truma.protocol import parse_v3_frame

_LOGGER = logging.getLogger(__name__)

# ONE bleak connect() is up to three BlueZ dials, not one: BlueZ gives up on a
# dial after ~21 s with "le-connection-abort-by-local", and bleak retries that
# error internally (bluezdbus/client.py, "retry due to le-connection-abort-by-
# local") rather than raising it. Measured on the van 2026-08-19: dial 13:14:38,
# abort 13:14:59, bleak's retry landed 13:15:01 and GATT resolved -- and a 30 s
# timeout tore it all down at 13:15:08, seven seconds after it had worked.
# So this must clear several full BlueZ dials, not one. Measured again with the
# timeout at 90 s: dial 13:48:07, BlueZ had the services resolved at 13:49:29,
# and 90 s still cut it off at 13:49:37. Every cycle burns one guaranteed-dead
# 20 s dial (the kernel patch puts the identity on air first -- see
# DUCATO_STATE.md, "the first dial of every cycle is wasted"), so the budget has
# to cover that plus the real one plus GATT discovery.
_CONNECT_TIMEOUT = 180.0

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
# How long to let BlueZ produce the link by itself before dialling. A bonded,
# trusted panel is reconnected in the background within seconds, and a dial we
# do not need is the thing that blinds the adapter -- so waiting is cheaper than
# asking.
_BLUEZ_GRACE = 20.0

# Long enough to cover the slowest honest recovery: after a dial is cleared,
# BlueZ disowns the link but the kernel keeps it, and bt-ghostbuster only drops
# that at its 2-minute mark -- BlueZ then reconnects within seconds. 150 s used
# to end the session just short of it.
_WATCH_SECONDS = 240.0
_WATCH_INTERVAL = 2.0
# One BlueZ dial is ~21 s, so retrying much faster than this only collects
# instant refusals; much slower wastes the window.
_BUSY_RETRY_INTERVAL = 20.0


def _client_is_proxy(client: object) -> bool:
    """Whether this client talks through an ESPHome proxy rather than BlueZ.

    Proxy clients come from ``bleak_esphome``; a local adapter gives BlueZ's
    ``bleak.backends.bluezdbus`` client. The two need opposite handling for
    pairing (see :meth:`TrumaBle._subscribe`), and the backend module is the
    only reliable way to tell them apart from here.
    """
    backend = getattr(client, "_backend", client)
    return "esphome" in type(backend).__module__

_READY_TIMEOUT = 3.0
_ACK_TIMEOUT = 3.0


# BlueZ errors that do not mean "this device is unreachable". Measured on the
# van: a Connect() that dials the peer's identity address first fails with a
# plain org.bluez.Error.Failed after ~20 s -- and in the same second BlueZ's own
# pending attempt lands, exports the GATT services and reports Connected: true.
# The caller has been told "no" about a link that exists. Retrying a few seconds
# later attaches to it; giving up leaves it owned by nobody, and a peripheral
# that believes it has a central stops advertising, so nothing can find it again.
_RETRYABLE = (
    "already in progress",
    "org.bluez.error.failed",
    "org.bluez.error.inprogress",
    "org.bluez.error.notready",
)


async def _bluez_holds_link(ble_device: BLEDevice) -> bool:
    """Whether BlueZ itself has this device connected right now.

    ``BleakClient.is_connected`` answers for *our* client, which is False even
    while BlueZ holds a fully resolved link -- the state this panel spends most
    of its time in. bleak skips its own ``Connect`` call when the device is
    already connected, so knowing this is what lets a retry adopt the link
    instead of starting a second connect that BlueZ then refuses.
    """
    path = (ble_device.details or {}).get("path")
    if not path:
        return False
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        return bool(manager.is_connected(path))
    except Exception as exc:  # noqa: BLE001 - not worth failing a connect over
        _LOGGER.debug("Truma connect: cannot read BlueZ link state: %s", exc)
        return False


async def _dbus_connect(ble_device: BLEDevice) -> None:
    """Ask BlueZ to connect, without a BleakClient behind the call.

    A ``BleakClient.connect()`` that we walk away from is not inert: whenever it
    eventually errors, times out or is cancelled, bleak runs its client cleanup,
    and that cleanup disconnects -- on the same device path the *adopted* client
    is using. Measured on the van 2026-08-19, twice, 130-185 ms apart each time:

        14:50:46.761  the abandoned dial ended: [org.bluez.Error.Failed] Input/output error
        14:50:46.890  BLE link closed cleanly

    A bare D-Bus method call has no client behind it, so nothing runs a cleanup
    on our behalf. That is not the same as being free to abandon it -- see
    :func:`_clear_dial` for what an unanswered dial costs at the kernel level.
    """
    await _device_call(ble_device, "Connect")


async def _device_call(ble_device: BLEDevice, member: str) -> None:
    """Call one no-argument method on the device's BlueZ object."""
    from dbus_fast import Message, MessageType
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager

    path = (ble_device.details or {}).get("path")
    if not path:
        raise BleakError(f"no BlueZ object path for {ble_device.address}")

    manager = await get_global_bluez_manager()
    bus = manager._bus  # noqa: SLF001 - the only handle on BlueZ's bus
    if bus is None:
        raise BleakError("BlueZ D-Bus connection is not up")

    reply = await bus.call(
        Message(
            destination="org.bluez",
            path=path,
            interface="org.bluez.Device1",
            member=member,
        )
    )
    if reply is None:
        raise BleakError(f"no reply from BlueZ to {member}")
    if reply.message_type is MessageType.ERROR:
        detail = reply.body[0] if reply.body else ""
        raise BleakError(f"[{reply.error_name}] {detail}")


async def _clear_dial(ble_device: BLEDevice, dial: asyncio.Task) -> None:
    """Cancel an outstanding dial, at BlueZ's end as well as ours.

    Cancelling the task alone achieves nothing: the D-Bus call is ours, but the
    ``att_io`` socket it made bluetoothd open is not, and that socket is what
    pins an ``hci_conn`` in ``BT_CONNECT`` and blinds the adapter.
    ``Device1.Disconnect()`` is the only thing that closes it -- verified on the
    van 2026-08-19 15:20, where it removed the stuck ``state 5`` entry, left the
    established link alone, and advertising reports went from 3/min to 218/min.
    """
    try:
        await _device_call(ble_device, "Disconnect")
    except Exception as exc:  # noqa: BLE001 - best effort, we still cancel ours
        _LOGGER.debug("Truma connect: clearing the dial failed: %s", exc)
    dial.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await dial


async def _bluez_link_ready(ble_device: BLEDevice) -> bool:
    """Whether BlueZ has the link up AND the GATT database exported.

    Stronger than :func:`_bluez_holds_link`: a client can only be attached once
    the services are resolved, so this is the condition for abandoning a connect
    call that is never going to return.
    """
    path = (ble_device.details or {}).get("path")
    if not path:
        return False
    try:
        from bleak.backends.bluezdbus import defs
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        props = manager._properties.get(path, {}).get(defs.DEVICE_INTERFACE, {})  # noqa: SLF001
        return bool(props.get("Connected")) and bool(props.get("ServicesResolved"))
    except Exception as exc:  # noqa: BLE001 - not worth failing a connect over
        _LOGGER.debug("Truma connect: cannot read BlueZ link state: %s", exc)
        return False


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


class ClearedDial(BleakError):
    """Raised when BlueZ produced the link before our dial was answered.

    Not a failure: the link exists. But our dial can no longer be answered and
    holding it open blinds the adapter, so it is cleared and the session
    restarted -- the retry then takes the attach path.
    """


def is_retryable_error(exc: BaseException) -> bool:
    """True when the connect may yet have worked, or is worth another go."""
    if isinstance(exc, ClearedDial):
        # The link is up; we cleared our own dial to stop it blinding the
        # adapter. Retrying attaches to what BlueZ already has.
        return True
    text = str(exc).lower()
    if "authentication" in text:
        # A bond problem. Retrying re-runs a failing pairing and, on this panel,
        # burns one of its four bond slots each time.
        return False
    return any(marker in text for marker in _RETRYABLE)


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
        self.assigned_addr = DEV_APP_DEFAULT

    def on_data(self, callback: Callable[[dict], None]) -> None:
        """Register a callback for decoded V3 frames."""
        self._data_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        """Whether the BLE link is up."""
        return self._client is not None and self._client.is_connected

    async def _connect_or_adopt(
        self,
        ble_device: BLEDevice,
        make_client: Callable[[], BleakClientWithServiceCache],
    ) -> BleakClientWithServiceCache:
        """Attach to BlueZ's link, and only dial if it does not produce one.

        Two things make this awkward, both measured on the van 2026-08-19.

        bluetoothd answers a ``Device1.Connect()`` from ``att_connect_cb()`` --
        the completion of the dial *it* started. When the link instead arrives
        on the accept path (its own background reconnect of a bonded device),
        that callback never runs, ``device->connect`` is never replied to, and
        the call hangs for good. No timeout can fix that.

        And an unanswered dial is not harmless. It holds bluetoothd's
        ``att_io`` open, which keeps an ``hci_conn`` in ``BT_CONNECT`` forever,
        which keeps the device in the kernel's connect list, which makes passive
        scanning accept-list filtered -- so the adapter goes **blind to every
        other device**. HA's scanner watchdog then power-cycles the controller
        and takes our link down with it. Renogy and the thermometers saw zero
        advertising reports for as long as the panel was connected.

        So: prefer BlueZ's own link and never dial when there is one; give it a
        grace period to produce one before dialling at all; and if a dial is
        outstanding when the link appears, clear it with ``Disconnect()`` rather
        than walking away from it. Clearing costs a session -- ``Disconnect()``
        drops BlueZ's ``Connected`` too -- but the caller retries within
        seconds and takes the attach path, which leaves nothing behind.
        """
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        dial_at = time.monotonic() + _BLUEZ_GRACE
        dial: asyncio.Task | None = None

        while True:
            if await _bluez_link_ready(ble_device):
                if dial is not None:
                    # BlueZ got there first and our dial will never be
                    # answered. Leaving it pending is what blinds the adapter.
                    _LOGGER.debug(
                        "Truma connect: BlueZ produced the link first; clearing "
                        "our unanswered dial and retrying"
                    )
                    await _clear_dial(ble_device, dial)
                    raise ClearedDial(f"cleared an unanswered dial to {ble_device.address}")
                _LOGGER.debug("Truma connect: BlueZ has the link; attaching")
                attached = make_client()
                await attached.connect()
                return attached

            if dial is not None and dial.done():
                await dial  # re-raise whatever it decided
                # A dial that was answered has produced a link but nothing we
                # can talk GATT over; the next turn attaches to it.
                dial = None
                continue

            if dial is None and time.monotonic() >= dial_at:
                _LOGGER.debug("Truma connect: no link from BlueZ; dialling")
                dial = asyncio.create_task(_dbus_connect(ble_device))

            if time.monotonic() >= deadline:
                if dial is not None:
                    await _clear_dial(ble_device, dial)
                raise TimeoutError(
                    f"no link to {ble_device.address} within {_CONNECT_TIMEOUT:.0f}s"
                )

            await asyncio.sleep(_WATCH_INTERVAL)

    async def connect(
        self,
        ble_device: BLEDevice,
        disconnected_callback: Callable[[BleakClientWithServiceCache], None]
        | None = None,
    ) -> None:
        """Establish the connection (via HA's stack) and subscribe."""
        self._loop = asyncio.get_running_loop()
        # Connect patiently, and do it ourselves: bleak_retry_connector hard-
        # codes a 20 s connect timeout that cannot be raised through
        # establish_connection(). Where the host has to guess the panel's
        # current address (no controller address resolution), the first dial
        # goes to a stale one and fails at ~20 s -- the kernel then retries on
        # its own and the link comes up a few seconds later. Give up before
        # that and the link is established with NO owner: the panel believes it
        # has a central and stops advertising, so nothing can reach it again.
        # Waiting is what keeps us the owner. See DUCATO_STATE.md.
        def _make_client() -> BleakClientWithServiceCache:
            return BleakClientWithServiceCache(
                ble_device,
                disconnected_callback=disconnected_callback,
                timeout=_CONNECT_TIMEOUT,
            )

        try:
            client = await self._connect_or_adopt(ble_device, _make_client)
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_retryable_error(exc):
                try:
                    await close_stale_connections_by_address(ble_device.address)
                except Exception as err:  # noqa: BLE001 - best effort
                    _LOGGER.debug("Truma stale-connection cleanup: %s", err)
                raise
            # Ask again in a moment. A dial made while BlueZ is genuinely busy
            # is refused instantly and costs nothing, and _connect_or_adopt
            # attaches without dialling at all once BlueZ has the link -- so
            # retrying is how both endings are reached. Purely *watching* was
            # measured to be worse: when the refusal came from a wedged ATT
            # bearer, bt-ghostbuster cleared it a minute later and nothing was
            # there to dial, so the whole 150 s window was spent idle
            # (van 2026-08-19 14:17:59 -> 14:20:29, one session lost for nothing).
            _LOGGER.debug(
                "Truma connect: BlueZ said %s; retrying until it lets us in",
                exc,
            )
            deadline = time.monotonic() + _WATCH_SECONDS
            while True:
                if time.monotonic() >= deadline:
                    try:
                        await close_stale_connections_by_address(ble_device.address)
                    except Exception as err:  # noqa: BLE001 - best effort
                        _LOGGER.debug("Truma stale-connection cleanup: %s", err)
                    raise
                await asyncio.sleep(_BUSY_RETRY_INTERVAL)
                try:
                    client = await self._connect_or_adopt(
                        ble_device, _make_client
                    )
                except Exception as retry_exc:  # noqa: BLE001 - classified here
                    if not is_retryable_error(retry_exc):
                        try:
                            await close_stale_connections_by_address(
                                ble_device.address
                            )
                        except Exception as err:  # noqa: BLE001 - best effort
                            _LOGGER.debug("Truma stale-connection cleanup: %s", err)
                        raise
                    continue
                break
        self._client = client
        await self._subscribe()

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
        pair_first = _client_is_proxy(self._client)
        _LOGGER.debug(
            "Truma subscribe: transport=%s, pair() %s",
            "proxy" if pair_first else "local",
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
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort
                _LOGGER.debug("Truma BLE disconnect error: %s", exc)

    # -- notifications ---------------------------------------------------

    def _notify_cmd(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_CMD, bytes(data))

    def _notify_data(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._handle_notification(CHAR_DATA_R, bytes(data))

    def _handle_notification(self, char_uuid: str, data: bytes) -> None:
        if len(data) <= 4:
            # MsgAck (0x83) must be auto-confirmed with 0x0300.
            if data and data[0] == TRANSPORT_MSG_ACK:
                self._fire_write(CHAR_CMD, bytes([TRANSPORT_CONFIRM, 0x00]))
            self._transport_ack = data
            if self._transport_event is not None:
                self._transport_event.set()
            return

        if char_uuid == CHAR_CMD:
            if self._transport_event is not None:
                self._transport_event.set()
            return

        # DATA_R: incoming V3 data frame — auto-ACK, parse, dispatch.
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
            self._loop.create_task(self._write(char_uuid, data))

    # -- sending ---------------------------------------------------------

    async def _write(self, char_uuid: str, data: bytes) -> None:
        if self._client is None:
            return
        # CMD uses Write Request (with response); DATA_W uses Write Command.
        response = char_uuid == CHAR_CMD
        await self._client.write_gatt_char(char_uuid, data, response=response)

    async def send(self, packet: bytes) -> bool:
        """Send a V3 packet through the transport FSM. Returns True on DataAck."""
        async with self._send_lock:
            return await self._send_locked(packet)

    async def _send_locked(self, packet: bytes) -> bool:
        success = False
        try:
            self._transport_event = asyncio.Event()
            self._transport_ack = None

            # 1. InitDataTransfer announce.
            announce = bytes(
                [TRANSPORT_INIT, len(packet) & 0xFF, (len(packet) >> 8) & 0xFF]
            )
            await self._write(CHAR_CMD, announce)

            # 2. Wait for Ready.
            try:
                await asyncio.wait_for(self._transport_event.wait(), _READY_TIMEOUT)
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for Ready")
            self._transport_event.clear()

            # 3. Send payload on DATA_W.
            await self._write(CHAR_DATA_W, packet)

            # 4. Wait for DataAck.
            try:
                await asyncio.wait_for(self._transport_event.wait(), _ACK_TIMEOUT)
                # DataAck (0xF0) or MsgAck (0x83) both mean the panel took the
                # frame — heater-routed writes reply MsgAck, panel writes DataAck.
                if self._transport_ack and self._transport_ack[0] in (
                    TRANSPORT_ACK,
                    TRANSPORT_MSG_ACK,
                ):
                    success = True
            except TimeoutError:
                _LOGGER.debug("Truma transport: timeout waiting for DataAck")

            # 5. Let any async MsgAck settle.
            await asyncio.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Truma transport error: %s", exc)
        finally:
            self._transport_event = None
            self._transport_ack = None
        return success
