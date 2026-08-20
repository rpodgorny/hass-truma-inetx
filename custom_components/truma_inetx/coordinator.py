"""Coordinator for the Truma iNet X (BLE) integration.

Owns the shared :class:`TrumaState` and a background session that connects to
the panel over HA's Bluetooth stack, runs the register/subscribe/identity/
param-discovery startup, and feeds notifications into the state. Reconnects
with backoff on drop.
"""

from __future__ import annotations

import asyncio
import uuid

from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble import TrumaBleClient, device_from_bluez
from .bt import (
    async_panel_advertising,
    async_resolve_proxy_device,
    async_wait_until_heard,
)
from .const import (
    DOMAIN,
    ISSUE_NO_PROXY_ROUTE,
    LOGGER,
    NO_PROXY_MISSES_BEFORE_WARNING,
)
from .truma.const import (
    CTRL_MBP,
    DEV_APP_DEFAULT,
    DEV_HEATER,
    DEV_PANEL,
    MBP_PARAM_DISC,
    TOPIC_BATCHES,
)
from .truma.protocol import (
    build_identity_frames,
    build_register_frame,
    build_subscribe_frame,
    build_v3_frame,
    build_write_frame,
)
from .truma.state import TrumaState

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]

# Reconnect backoff. Start quick (a healthy link that just dropped should come
# back fast) and grow exponentially to a cap when the panel stays unreachable,
# so an out-of-range/unbonded device does not hammer — and monopolize — the
# shared Bluetooth adapter. The delay resets after any session that connected.
_RECONNECT_DELAY_BASE = 15  # seconds
# Keep the cap short. A dial that times out is not the end of the story on a
# host without address resolution: the kernel keeps trying and the link can
# come up seconds after we gave up, owned by nobody -- and a panel that thinks
# it has a central stops advertising, so the longer we wait the deeper that
# hole gets. Retrying soon is what re-attaches to such a link.
_RECONNECT_DELAY_MAX = 45  # seconds
# A healthy panel pushes frames every few seconds. If a connection goes quiet
# for this long the link is wedged (half-open, or a ghost the proxy has not
# noticed): drop it and reconnect rather than sit "connected" forever with
# stale data. This is what recovers the session without a manual power-cycle.
_DATA_STALL_TIMEOUT = 90  # seconds

# Poll mode: 0 keeps the link open (the default and what most people want --
# state arrives the instant the panel changes it). A non-zero interval connects,
# takes a reading and hangs up again, which matters when the adapter's
# connection slots are contended: a held link occupies one permanently, and on a
# single dongle shared with other devices that can starve them out entirely
# (van 2026-08-20: the DC-DC charger lost its slot and went unavailable).
#
# Seconds, not minutes: a minute is already coarse next to a poll that takes
# only a few seconds, and the interesting settings are near the bottom of the
# range. The delay is applied *after* a poll finishes, so a short interval
# cannot make polls overlap -- it just leaves less idle time between them.
CONF_POLL_INTERVAL = "poll_interval_seconds"
DEFAULT_POLL_INTERVAL = 0

# In poll mode, stop waiting once the panel has been quiet this long -- its
# startup burst arrives in one go, so silence means the reading is complete.
_POLL_QUIET = 4  # seconds
# ...but never hold the link longer than this, however chatty the panel is.
_POLL_MAX_DWELL = 40  # seconds
_STORAGE_VERSION = 1


class TrumaCoordinator(DataUpdateCoordinator[TrumaState]):
    """Hold Truma state and run the live BLE session."""

    config_entry: TrumaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TrumaConfigEntry,
        address: str,
        initial_client: BleakClientWithServiceCache | None = None,
    ) -> None:
        """Initialize the coordinator (push model, no polling interval).

        ``initial_client`` is a live, encrypted connection handed off from a
        just-completed pairing; the first session adopts it instead of
        reconnecting (which wedges the just-bonded RPA). Consumed once.
        """
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {address}",
            update_interval=None,
        )
        self.address = address
        self._initial_client = initial_client
        # Stable identity for entity/device unique IDs. The BLE address rotates
        # (resolvable private address), so it must NOT be used as identity.
        self.unique_id = entry.unique_id or address
        self._state = TrumaState()
        self._client: TrumaBleClient | None = None
        self._identity: dict | None = None
        # Loop-clock timestamp of the last frame received; drives the stall
        # watchdog in the hold loop. Set on connect, refreshed on every frame.
        self._last_frame: float = 0.0
        # RPA addresses that failed to establish a connection, so the resolver
        # rotates to another advertised address instead of hammering a dead one
        # (see the phantom-RPA explanation in bt.async_resolve_proxy_device).
        # Cleared on a successful connection and when it would block every
        # candidate, so a transiently-bad address gets retried later.
        self._avoid: set[str] = set()
        # Address of the most recent connection attempt, so _run knows which
        # one to blame if the attempt fails.
        self._last_addr: str | None = None
        # Consecutive resolves that found the panel advertising but no proxy
        # able to reach it. Debounces the repair issue (see _async_note_...).
        self._no_proxy_misses = 0
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        self._stop = False
        # Set on stop to interrupt the reconnect wait immediately (so unload is
        # not blocked for up to the full backoff delay).
        self._stop_event = asyncio.Event()

    async def _async_update_data(self) -> TrumaState:
        """Return the current shared state (updated by BLE notifications)."""
        return self._state

    def _async_note_no_proxy_route(self) -> None:
        """Warn the user when the panel is audible but unreachable.

        The panel uses a rotating private address, so reconnecting needs the
        peer's current address to be put on air. A Bluetooth proxy's controller
        resolves that itself; a local adapter can only do it if its controller
        supports LL Privacy (most USB dongles and the Raspberry Pi's built-in
        adapter do not -- check with `btmon` for "Resolving List" support) or
        the host kernel compensates. Such a setup pairs once and then never
        reconnects, which looks like a broken integration rather than missing
        hardware. Say so instead of failing silently.
        """
        if not async_panel_advertising(self.hass, self.unique_id):
            # We cannot hear the panel at all -- off, asleep or out of range.
            # Telling this user to buy a proxy would be wrong, so stay quiet
            # and do not let it count towards the warning either.
            return
        self._no_proxy_misses += 1
        if self._no_proxy_misses != NO_PROXY_MISSES_BEFORE_WARNING:
            # Fires exactly once on the way up, so repeated failures do not
            # re-create the issue and re-notify every reconnect attempt.
            return
        LOGGER.warning(
            "Truma %s is advertising but no route can reach it; a Bluetooth "
            "proxy resolves the panel's rotating address for you, whereas a "
            "local adapter needs controller or kernel support for it",
            self.unique_id,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NO_PROXY_ROUTE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_PROXY_ROUTE,
            learn_more_url="https://esphome.io/components/bluetooth_proxy.html",
        )

    def _async_clear_no_proxy_route(self) -> None:
        """Reset the miss counter and drop the issue if it was raised."""
        self._no_proxy_misses = 0
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_PROXY_ROUTE)

    async def async_start(self) -> None:
        """Load identity and launch the background BLE session."""
        self._identity = await self._load_identity()
        self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} session {self.address}"
        )

    async def async_stop(self) -> None:
        """Stop the session and disconnect."""
        self._stop = True
        self._stop_event.set()
        await self._disconnect_client()

    async def _disconnect_client(self) -> None:
        """Disconnect and drop the current BLE client, best effort.

        Frees the proxy connection slot so the next attempt starts clean.
        """
        client = self._client
        self._client = None
        if client is None:
            LOGGER.debug("Truma %s: no live BLE link to close", self.unique_id)
            return
        try:
            await client.disconnect()
            LOGGER.debug("Truma %s: BLE link closed cleanly", self.unique_id)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            LOGGER.debug("Truma %s disconnect: %s", self.unique_id, exc)

    async def _load_identity(self) -> dict:
        """Load the persisted app identity, or create and store a new one."""
        data = await self._store.async_load()
        if not data:
            data = {
                "muid": str(uuid.uuid4()).upper(),
                "uuid": str(uuid.uuid4()).lower(),
                "username": "Home Assistant",
            }
            await self._store.async_save(data)
        return data

    async def _run(self) -> None:
        """Maintain the BLE session, reconnecting with exponential backoff."""
        delay = _RECONNECT_DELAY_BASE
        while not self._stop:
            connected = False
            try:
                connected = await self._connect_and_run()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Truma session ended: %s", exc)
                # If the attempt never got a link up, banish that address so the
                # resolver rotates to another advertised RPA next round instead
                # of hammering a post-pairing phantom (see bt.py). Only a failed
                # *connect* leaves _last_addr set; a later failure clears it.
                if self._last_addr:
                    self._avoid.add(self._last_addr)
            finally:
                # Always tear the client down before the next attempt so a
                # half-open link never lingers holding the proxy's connection
                # slot (the ghost that otherwise needs a manual power-cycle).
                await self._disconnect_client()
            self._mark_disconnected()
            if self._stop:
                break
            # A session that actually connected resets the backoff (a healthy
            # link that just dropped should return fast); a failed attempt grows
            # it after the wait, so a persistently unreachable panel backs off
            # the shared adapter instead of hammering it.
            if connected and self.poll_interval:
                # A completed poll is not a failure to back off from; the next
                # one is simply due later.
                delay = self.poll_interval
            elif connected:
                delay = _RECONNECT_DELAY_BASE
                # A real connection means our address set is healthy; forget any
                # past failures so a later reconnect starts from a clean slate.
                self._avoid.clear()
            LOGGER.debug("Truma %s reconnecting in %ss", self.unique_id, delay)
            await self._wait_before_retry(delay)
            if not connected:
                delay = min(delay * 2, _RECONNECT_DELAY_MAX)

    async def _wait_before_retry(self, delay: float) -> None:
        """Sleep ``delay`` seconds, but wake immediately on stop."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _connect_and_run(self) -> bool:
        """Connect, run startup, then hold until the link drops.

        Returns ``True`` once the connection was established (so the caller
        resets the backoff). Raises if the connection could not be established.
        """
        assert self._identity is not None
        client = TrumaBleClient(self._identity)
        client.on_data(self._on_frame)
        # Track the client before connecting so a failed/partial connect is
        # still torn down by _run's finally (freeing the proxy slot).
        self._client = client

        # First attempt after a fresh pairing: adopt the live connection the
        # config flow handed off, instead of reconnecting. This is what avoids
        # the post-pairing RPA wedge — never disconnect the bonded link.
        initial = self._initial_client
        self._initial_client = None  # consume: adopt only once
        if initial is not None:
            if initial.is_connected:
                LOGGER.debug(
                    "Truma %s: adopting handed-off pairing connection %s",
                    self.unique_id,
                    initial.address,
                )
                self._last_addr = None
                await client.adopt(initial)
                return await self._finish_startup(client)
            # Handed-off link dropped in the setup gap — discard and connect
            # fresh below.
            LOGGER.debug(
                "Truma %s: handed-off connection was already closed; "
                "connecting fresh",
                self.unique_id,
            )
            try:
                await initial.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort
                LOGGER.debug("Truma %s stale handoff disconnect: %s", self.unique_id, exc)

        ble_device = async_resolve_proxy_device(
            self.hass, self.unique_id, avoid=self._avoid
        )
        if ble_device is None and self._avoid:
            # Every address we know about has failed to establish. Rather than
            # stay stuck reporting "not advertising", forget the failures and
            # start over — the phantom may have cleared, or the panel may have
            # rotated back to a usable RPA.
            LOGGER.debug(
                "Truma %s: all candidates avoided; clearing and retrying",
                self.unique_id,
            )
            self._avoid.clear()
            ble_device = async_resolve_proxy_device(self.hass, self.unique_id)
        if ble_device is None:
            # Silence usually means the opposite of unreachable: BlueZ is
            # already holding a link, so the panel has a central and stops
            # advertising. Take BlueZ's own device object and attach to it.
            ble_device = await device_from_bluez(self.unique_id)
            if ble_device is not None:
                LOGGER.debug(
                    "Truma %s: not advertising, but BlueZ has the device; "
                    "attaching to its object",
                    self.unique_id,
                )
        if ble_device is None:
            self._async_note_no_proxy_route()
            raise HomeAssistantError(
                f"Truma {self.unique_id} not currently advertising"
            )
        # A resolve that succeeded disproves the issue outright: something
        # connectable reached the panel. (The adopted-handoff path above needs
        # no equivalent -- it only happens straight after pairing, which itself
        # required a proxy route, so the issue cannot already be raised.)
        self._async_clear_no_proxy_route()
        self._last_addr = ble_device.address
        # Dial while the panel is still audible: the resolved address is only
        # good for as long as the host's cache of it is (see
        # bt.async_wait_until_heard). A stale dial costs a ~20 s timeout during
        # which nothing scans, so it keeps itself stale.
        if not await async_wait_until_heard(self.hass, self.unique_id):
            # Silence is not a reason to give up: the commonest cause of it is
            # that something already holds a link to the panel, and a panel
            # with a central does not advertise. Connecting then costs nothing
            # and attaches to that link instead of leaving it unused, which is
            # exactly the hole a wait-only gate digs. A genuinely absent panel
            # costs one connect timeout.
            LOGGER.debug(
                "Truma %s: connecting without a fresh advert", self.unique_id
            )
        await client.connect(ble_device)
        # The connection established, so this address is not the phantom —
        # clear the blame marker so a later failure (startup, a mid-session
        # drop) does not wrongly banish a perfectly good address.
        self._last_addr = None

        return await self._finish_startup(client)

    @property
    def poll_interval(self) -> int:
        """Seconds between polls, or 0 to hold the connection open."""
        return int(
            self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )

    async def _finish_startup(self, client: TrumaBleClient) -> bool:
        """Run startup on a connected client, then hold until the link drops.

        Shared by the fresh-connect and adopted-handoff paths. Returns ``True``
        (the connection is up, so the caller resets the backoff).
        """
        await self._run_startup(client)

        self._state.connected = True
        self._state.assigned_addr = client.assigned_addr
        self.async_set_updated_data(self._state)
        LOGGER.info("Truma %s connected and subscribed", self.unique_id)

        # Startup just delivered frames, so seed the watchdog from now.
        self._last_frame = self.hass.loop.time()

        if self.poll_interval:
            # Poll mode: the reading is in hand, so let the link go and free the
            # connection slot. Wait only until the panel stops talking.
            started = self.hass.loop.time()
            while not self._stop and client.connected:
                await asyncio.sleep(1)
                quiet = self.hass.loop.time() - self._last_frame
                if quiet >= _POLL_QUIET:
                    break
                if self.hass.loop.time() - started >= _POLL_MAX_DWELL:
                    LOGGER.debug(
                        "Truma %s: still talking after %ss; ending the poll anyway",
                        self.unique_id,
                        _POLL_MAX_DWELL,
                    )
                    break
            LOGGER.debug(
                "Truma %s: poll complete, disconnecting for %ss",
                self.unique_id,
                self.poll_interval,
            )
            return True

        # Connected mode: hold the link, watching for a data stall.
        while not self._stop and client.connected:
            await asyncio.sleep(1)
            if self.hass.loop.time() - self._last_frame > _DATA_STALL_TIMEOUT:
                LOGGER.warning(
                    "Truma %s: no data for %ss; link is stale, reconnecting",
                    self.unique_id,
                    _DATA_STALL_TIMEOUT,
                )
                break
        return True

    async def _run_startup(self, client: TrumaBleClient) -> None:
        """Register, subscribe to all topics, send identity, discover params."""
        # 1. Register and wait for an assigned address.
        await client.send(build_register_frame(client.assigned_addr))
        for _ in range(20):
            await asyncio.sleep(1)
            if client.assigned_addr != DEV_APP_DEFAULT:
                break

        # 2. Subscribe to all topic batches.
        for batch in TOPIC_BATCHES:
            await client.send(build_subscribe_frame(client.assigned_addr, batch))
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)

        # 3. Send the identity sequence.
        for frame in build_identity_frames(client.assigned_addr, self._identity):
            await client.send(frame)
            await asyncio.sleep(0.5)

        # 4. Request current values from heater and panel.
        for dev_addr in (DEV_HEATER, DEV_PANEL):
            await client.send(
                build_v3_frame(
                    dev_addr, client.assigned_addr, CTRL_MBP, MBP_PARAM_DISC, 0, b""
                )
            )
            await asyncio.sleep(3)

    @callback
    def _on_frame(self, parsed: dict) -> None:
        """Handle a decoded V3 frame and update state."""
        # Any frame proves the link is alive; feed the stall watchdog.
        self._last_frame = self.hass.loop.time()
        control = parsed.get("control_raw")
        sub_type = parsed.get("sub_type")
        cbor = parsed.get("cbor")
        if not isinstance(cbor, dict):
            return

        # Registration response -> assigned address.
        if control == 0x01 and sub_type == 0x02:
            addr = cbor.get("addr")
            if addr and self._client is not None:
                self._client.assigned_addr = addr
                self._state.assigned_addr = addr
            return

        # Info message -> single parameter update.
        if control == 0x03 and sub_type == 0x00:
            tn, pn, v = cbor.get("tn"), cbor.get("pn"), cbor.get("v")
            if tn and pn and v is not None:
                self._state.update(tn, pn, v)
                self.async_set_updated_data(self._state)
            return

        # Parameter-discovery response -> nested current values.
        if control == 0x03 and sub_type == 0x84:
            for topic in cbor.get("topics", []) or []:
                if not isinstance(topic, dict):
                    continue
                tn = topic.get("tn", "")
                for param in topic.get("parameters", []) or []:
                    if not isinstance(param, dict):
                        continue
                    pn, v = param.get("pn"), param.get("v")
                    if tn and pn and v is not None:
                        self._state.update(tn, pn, v)
            self.async_set_updated_data(self._state)
            return

    @callback
    def _mark_disconnected(self) -> None:
        """Flag the link as down and notify entities."""
        if self._state.connected:
            self._state.connected = False
            self.async_set_updated_data(self._state)

    async def async_write(self, topic: str, param: str, value: int) -> None:
        """Validate and send a parameter write to the panel/heater.

        The panel confirms by pushing an updated value, which flows back through
        the normal notification path and updates the entity.
        """
        ok, msg = TrumaState.validate_command(topic, param, value)
        if not ok:
            raise HomeAssistantError(f"Invalid Truma command: {msg}")

        client = self._client
        if client is None or not client.connected:
            raise HomeAssistantError("Truma panel is not connected")

        dest = TrumaState.get_command_dest(topic)
        frame = build_write_frame(client.assigned_addr, dest, topic, param, value)
        LOGGER.debug("Truma write %s.%s = %s -> 0x%04X", topic, param, value, dest)
        if not await client.send(frame):
            raise HomeAssistantError(
                f"Truma did not acknowledge write {topic}.{param}={value}"
            )
