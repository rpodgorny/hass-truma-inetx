"""Coordinator for the Truma iNet X (BLE) integration.

Owns the shared :class:`Bus` and a background session that connects to the
panel over HA's Bluetooth stack, runs the register/subscribe/identity/
param-discovery startup, and files every notification under the device that
sent it. Reconnects with backoff on drop.
"""

from __future__ import annotations

import asyncio
import uuid

from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import session
from .ble import TrumaBleClient, device_from_bluez
from .bt import (
    ADDR_IDENTITY,
    address_kind,
    async_panel_advertising,
    async_resolve_device,
    async_wait_until_heard,
)
from .bus import Bus
from .const import (
    DOMAIN,
    ISSUE_NO_PROXY_ROUTE_LEGACY,
    ISSUE_NO_ROUTE,
    LOGGER,
    MANUFACTURER,
    MODEL,
    NO_ROUTE_MISSES_BEFORE_WARNING,
)
from .truma.const import DEV_BLE_MGMT, DEV_PANEL
from .truma.protocol import build_write_frame

type TrumaConfigEntry = ConfigEntry[TrumaCoordinator]

# Names for bus addresses whose function is known but which publish no
# Identify.Name of their own.
#
# Consulted only *after* the bus's own name, so nothing that names itself can
# be mislabelled by this -- which is the whole reason it is safe, because
# 0x06 is a class the gas-bottle sensors share and they do name themselves.
# It is not a class-to-product mapping: those are unverifiable (a Dometic roof
# air conditioner and a Schaudt electrical block share a class), whereas this
# address is the panel's own Bluetooth side and is already named in
# DEVICE_SEED for the same reason.
_KNOWN_NAMES = {DEV_BLE_MGMT: "Bluetooth management"}

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
# A write in poll mode has to wait for a whole connect plus startup handshake
# (~20 s measured), so allow generously more than that before giving up.
_WRITE_CONNECT_TIMEOUT = 75  # seconds
_STORAGE_VERSION = 1

# How often to ask the on-demand sensors for a fresh measurement while the
# link is held open (see session.request_measurements for why asking is needed
# at all). A minute is what the reporter's own build used, which is the only
# cadence anyone has run against the hardware; it is also about as often as a
# tank level can meaningfully change, and it costs two frames.
#
# In poll mode this is not used: every poll re-runs startup, which asks once,
# so the reading is as fresh as the poll it came with.
_MEASURE_INTERVAL = 60  # seconds


class TrumaCoordinator(DataUpdateCoordinator[Bus]):
    """Hold the panel's bus and run the live BLE session."""

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
        self._bus = Bus()
        self._client: TrumaBleClient | None = None
        self._identity: dict | None = None
        # Loop-clock timestamp of the last frame received; drives the stall
        # watchdog in the hold loop. Set on connect, refreshed on every frame.
        self._last_frame: float = 0.0
        # RPA addresses that failed to establish a connection, so the resolver
        # rotates to another advertised address instead of hammering a dead one
        # (see the phantom-RPA explanation in bt.async_resolve_device).
        # Cleared on a successful connection and when it would block every
        # candidate, so a transiently-bad address gets retried later.
        self._avoid: set[str] = set()
        # Address of the most recent connection attempt, so _run knows which
        # one to blame if the attempt fails.
        self._last_addr: str | None = None
        # Which kind of address (identity vs rotating RPA) the panel last
        # answered on, persisted with the identity. Hosts differ in which one
        # works -- it depends on their kernel and controller, not on anything
        # we can see -- so let the host prove its own answer once instead of
        # walking the addresses that never work on every single connect
        # (issue #13). ``None`` until a session has proved something.
        self._address_kind: str | None = None
        # The kind the current attempt is dialling, and whether the memory
        # above just failed us (in which case the next attempt tries the other
        # kind -- see _prefer_identity).
        self._last_kind: str | None = None
        self._kind_stale = False
        # Whether the current attempt ever reached "connected and subscribed".
        # A link that dropped after hours of good service says nothing about
        # which address kind is right, so only failures before that flip the
        # memory.
        self._session_ok = False
        # Consecutive resolves that found the panel advertising with nothing
        # able to connect to it. Debounces the repair issue (see
        # _async_note_no_route).
        self._no_route_misses = 0
        self._store: Store = Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}")
        # Everything the store holds: the app identity plus our own
        # bookkeeping. Kept whole so a save never drops a key it did not know.
        self._stored: dict = {}
        self._stop = False
        # Set on stop to interrupt the reconnect wait immediately (so unload is
        # not blocked for up to the full backoff delay).
        self._stop_event = asyncio.Event()
        # Poll mode: a write cannot wait for the next scheduled poll, so it
        # nudges the loop awake and holds the link open until it has been sent.
        self._wake_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._writes_pending = 0

    async def _async_update_data(self) -> Bus:
        """Return the shared bus (updated by BLE notifications)."""
        return self._bus

    def _async_note_no_route(self) -> None:
        """Warn the user when the panel is audible but nothing can connect.

        The panel uses a rotating private address, and it only answers a
        connect that puts its current address on air. Whether a given adapter
        does that is a property of the host: a controller with LL Privacy and
        the panel's key in its resolving list does it, a kernel below 6.19 does
        it in software, an ESPHome proxy's controller does it. A host where
        none of them applies pairs once and then never reconnects, which looks
        like a broken integration rather than a Bluetooth setup that cannot
        serve this panel. Say so instead of failing silently.

        What to *do* about it is deliberately not prescribed here beyond the
        facts: this integration does not choose the adapter -- Home Assistant
        scores every path it has and picks -- so it is in no position to say
        which piece of the user's setup is the wrong one.
        """
        if not async_panel_advertising(self.hass, self.unique_id):
            # We cannot hear the panel at all -- off, asleep or out of range.
            # That is a different fault with different advice, so stay quiet
            # and do not let it count towards the warning either.
            return
        self._no_route_misses += 1
        if self._no_route_misses != NO_ROUTE_MISSES_BEFORE_WARNING:
            # Fires exactly once on the way up, so repeated failures do not
            # re-create the issue and re-notify every reconnect attempt.
            return
        LOGGER.warning(
            "Truma %s is advertising but nothing Home Assistant can reach it "
            "with is able to connect",
            self.unique_id,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_NO_ROUTE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_ROUTE,
            learn_more_url=(
                "https://github.com/rpodgorny/hass-truma-inetx"
                "#reaching-the-panel"
            ),
        )

    def _async_clear_no_route(self) -> None:
        """Reset the miss counter and drop the issue if it was raised."""
        self._no_route_misses = 0
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_ROUTE)
        # An issue this integration raised under the old id would otherwise
        # outlive the rename, showing the user a card with no text behind it.
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_PROXY_ROUTE_LEGACY)

    async def async_start(self) -> None:
        """Load identity and launch the background BLE session."""
        await self._load_stored_state()
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

        Frees the connection slot on whichever adapter or proxy carried it,
        so the next attempt starts clean.
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

    async def _load_stored_state(self) -> None:
        """Load the persisted app identity and address-kind memory.

        The identity is created and stored on first run; the memory is written
        only once a session has proved one (see _remember_address_kind), so it
        is absent on a fresh install and the resolver keeps its default order.
        """
        data = await self._store.async_load()
        if not data:
            data = {
                "muid": str(uuid.uuid4()).upper(),
                "uuid": str(uuid.uuid4()).lower(),
                "username": "Home Assistant",
            }
            await self._store.async_save(data)
        self._stored = data
        # Hand the protocol only what it writes to the panel: the stored blob
        # also carries our own bookkeeping, which is none of its business.
        self._identity = {
            key: data[key] for key in ("muid", "uuid", "username") if key in data
        }
        self._address_kind = data.get("address_kind")

    def _prefer_identity(self) -> bool:
        """Whether to dial the identity address before the RPAs.

        With no memory, keep the old order (RPAs first). With one, follow it --
        unless it just failed, in which case try the other kind next. That
        alternation is what makes a wrong memory cost one attempt rather than
        the connection: a host that loses the route it learned (a kernel
        upgrade taking the local adapter away, a proxy that moved) finds the
        other one by itself instead of looking like broken hardware.
        """
        if self._address_kind is None:
            return False
        prefer = self._address_kind == ADDR_IDENTITY
        return not prefer if self._kind_stale else prefer

    async def _remember_address_kind(self, kind: str) -> None:
        """Persist the kind of address that just carried a session.

        Persisted rather than kept in memory because the cost this avoids is
        paid at startup: the reporter in issue #13 measured 11.5 minutes from
        HA start to a live session, against 1.2 minutes when the working
        address was dialled first.
        """
        self._kind_stale = False
        if kind == self._address_kind:
            return
        LOGGER.debug(
            "Truma %s: connects on the %s address; remembering it",
            self.unique_id,
            kind,
        )
        self._address_kind = kind
        self._stored["address_kind"] = kind
        await self._store.async_save(self._stored)

    async def _run(self) -> None:
        """Maintain the BLE session, reconnecting with exponential backoff."""
        delay = _RECONNECT_DELAY_BASE
        while not self._stop:
            connected = False
            try:
                connected = await self._connect_and_run()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Truma session ended: %s", exc)
                self._note_attempt_failed()
            finally:
                # Always tear the client down before the next attempt so a
                # half-open link never lingers holding a connection slot (the
                # ghost that otherwise needs a manual power-cycle).
                await self._disconnect_client()
                self._connected_event.clear()
            if connected and self.poll_interval and not self._stop:
                # Poll mode: the link going away is the plan, not a fault. The
                # reading we just took is still the current state, so leave the
                # entities alone -- flagging disconnected here made every value
                # flash up and then go "unavailable" until the next poll.
                LOGGER.debug(
                    "Truma %s: poll finished, staying available until the next one",
                    self.unique_id,
                )
            else:
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

    def _note_attempt_failed(self) -> None:
        """Learn from an attempt that ended badly, at both levels.

        The address: if the attempt never got a link up, demote it so the
        resolver rotates to another advertised RPA next round instead of
        hammering a post-pairing phantom (see bt.py). It is a demotion, not a
        ban -- when it is the only route left the resolver still hands it back,
        which is what a transient failure (the panel holding the slot of a
        just-closed session) needs. Only a failed *connect* leaves
        ``_last_addr`` set; a later failure clears it.

        The address KIND: if the kind we remember just failed to carry a
        session, ignore the memory next time and try the other one; if it was
        the other kind that failed, go back to the memory. Alternating is what
        keeps a stale memory from wedging a host whose working route changed --
        a kernel upgrade taking the local adapter away, say. A session that ran
        and then dropped teaches nothing here, so ``_session_ok`` gates it.
        """
        if self._last_addr:
            self._avoid.add(self._last_addr)
        if not self._session_ok and self._last_kind is not None:
            self._kind_stale = self._last_kind == self._address_kind

    async def _wait_before_retry(self, delay: float) -> None:
        """Sleep ``delay`` seconds; wake early on stop, or for a pending write.

        ``_writes_pending`` is the source of truth and ``_wake_event`` only the
        nudge, so a write that lands in the gap between sessions cannot be
        missed by clearing the event at the wrong moment.
        """
        if self._writes_pending:
            return
        self._wake_event.clear()
        if self._writes_pending:  # set while we were clearing
            return
        stop = asyncio.ensure_future(self._stop_event.wait())
        wake = asyncio.ensure_future(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop, wake}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop.cancel()
            wake.cancel()

    async def _connect_and_run(self) -> bool:
        """Connect, run startup, then hold until the link drops.

        Returns ``True`` once the connection was established (so the caller
        resets the backoff). Raises if the connection could not be established.
        """
        assert self._identity is not None
        # Nothing has been dialled or proved yet this round. Clearing the kind
        # matters: an attempt that ends before it picks an address (the panel
        # silent, nothing connectable) is not evidence about address kinds, and
        # last round's kind left lying here would flip the memory on it.
        self._session_ok = False
        self._last_kind = None
        client = TrumaBleClient(self._identity)
        client.on_data(self._on_frame)
        # Track the client before connecting so a failed/partial connect is
        # still torn down by _run's finally (freeing the connection slot).
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
                # An adopted link was dialled by the config flow, not by us,
                # so it says nothing about which address kind this host
                # connects on.
                self._last_kind = None
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

        ble_device = async_resolve_device(
            self.hass,
            self.unique_id,
            avoid=self._avoid,
            prefer_identity=self._prefer_identity(),
        )
        if ble_device is None and self._avoid:
            # Nothing is on air at all, so the grudges are about addresses the
            # panel no longer uses. Drop them: a set that only ever grew would
            # keep demoting whatever the panel comes back on. (It cannot be
            # "everything was avoided" — avoid only demotes, so a reachable
            # address is always returned; see bt.async_resolve_device.)
            LOGGER.debug(
                "Truma %s: nothing advertising; forgetting past failures",
                self.unique_id,
            )
            self._avoid.clear()
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
            self._async_note_no_route()
            raise HomeAssistantError(
                f"Truma {self.unique_id} not currently advertising"
            )
        # A resolve that succeeded disproves the issue outright: something
        # connectable reached the panel. (The adopted-handoff path above needs
        # no equivalent -- it only happens straight after pairing, which itself
        # required a working route, so the issue cannot already be raised.)
        self._async_clear_no_route()
        self._last_addr = ble_device.address
        self._last_kind = address_kind(self.unique_id, ble_device.address)
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
    def address_kind(self) -> str | None:
        """Which kind of address the panel last answered on, if known.

        Exposed for diagnostics: it is the one piece of per-host state this
        integration learns, and a download that does not say which address kind
        a host settled on cannot explain its connect times.
        """
        return self._address_kind

    def device_info(self, addr: int) -> DeviceInfo:
        """The Home Assistant device an entity at a bus address belongs to.

        One HA device per bus address that has published something, hanging
        off the panel. That is what the panel is: a gateway onto a TIN bus of
        Truma appliances, a CI bus of vehicle electrics and third-party air
        conditioners, a CAN bus and Bluetooth gas sensors. Folding all of it
        into a single device -- which is what this did, with the model
        hard-coded to "iNet X (Combi)" -- meant a roof air conditioner's fan
        and a Combi's fan were two entities on one device with nothing saying
        which was which.

        The panel itself is that hub rather than a device below it. It is the
        thing this integration holds a Bluetooth link to, so giving it a
        second HA device beside the config entry's own would list the same
        appliance twice. DEV_PANEL is assumed to be the panel's address, as it
        is everywhere else here; if some installation numbers it differently
        the cost is one extra device, not a broken one.

        Names come from what the bus says: the panel's own name for a device
        under Identify.Name, plus its instance where the panel is using more
        than the first of a class (two gas-bottle sensors are 0x0603 and
        0x0604, and "Truma LevelControl" twice would be no better than the
        flat reading that mixed them up). A device that publishes no name is
        named by its address rather than by a class-to-product mapping that
        cannot be verified -- a Dometic roof air conditioner and a Schaudt
        electrical block share a device class. The one exception is
        _KNOWN_NAMES, and it is an exception only for addresses that name
        nothing themselves.
        """
        if addr == DEV_PANEL:
            panel = self._bus.devices.get(addr)
            return DeviceInfo(
                identifiers={(DOMAIN, self.unique_id)},
                # The advertised BLE name, not the panel's Identify.Name:
                # two panels in one Home Assistant would otherwise be two
                # devices with the same name.
                name=self.unique_id,
                manufacturer=MANUFACTURER,
                model=(panel.name if panel else None) or MODEL,
                serial_number=panel.serial if panel else None,
            )
        device = self._bus.device(addr)
        base = device.name or _KNOWN_NAMES.get(addr)
        if base is None:
            # Already unique, and already says where it is.
            name = f"Bus device 0x{addr:04X}"
        elif device.instance <= 1:
            name = base
        else:
            name = f"{base} {device.instance}"
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.unique_id}_{addr:04X}")},
            name=name,
            model=device.name,
            serial_number=device.serial,
            via_device=(DOMAIN, self.unique_id),
        )

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
        self._session_ok = True
        if self._last_kind is not None:
            # The panel answered, encrypted and subscribed on this address, so
            # this is the kind that works here. Connecting alone would not have
            # been proof: a path can establish a link and then fail to encrypt.
            await self._remember_address_kind(self._last_kind)

        # In poll mode this stays True between polls: it means "we are in
        # touch with the panel", not "a link is open this instant". The link
        # coming and going every interval is an implementation detail and
        # should not flap the connectivity sensor or blank every entity.
        self._bus.connected = True
        self._bus.assigned_addr = client.assigned_addr
        self.async_set_updated_data(self._bus)
        LOGGER.info("Truma %s connected and subscribed", self.unique_id)
        self._connected_event.set()

        # Startup just delivered frames, so seed the watchdog from now.
        self._last_frame = self.hass.loop.time()

        if self.poll_interval:
            # Poll mode: the reading is in hand, so let the link go and free the
            # connection slot. Wait only until the panel stops talking.
            started = self.hass.loop.time()
            while not self._stop and client.connected:
                await asyncio.sleep(1)
                if self._writes_pending:
                    # Someone is mid-write; do not hang up under them.
                    started = self.hass.loop.time()
                    continue
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

        # Connected mode: hold the link, watching for a data stall and keeping
        # the on-demand sensors measuring.
        next_measure = self.hass.loop.time() + _MEASURE_INTERVAL
        while not self._stop and client.connected:
            await asyncio.sleep(1)
            now = self.hass.loop.time()
            if now >= next_measure:
                # Schedule from now rather than from the previous slot: a send
                # that blocks on its acknowledgement must not leave a backlog
                # of missed slots to fire back to back.
                next_measure = now + _MEASURE_INTERVAL
                await self._request_measurements(client)
            if self.hass.loop.time() - self._last_frame > _DATA_STALL_TIMEOUT:
                LOGGER.warning(
                    "Truma %s: no data for %ss; link is stale, reconnecting",
                    self.unique_id,
                    _DATA_STALL_TIMEOUT,
                )
                break
        return True

    async def _run_startup(self, client: TrumaBleClient) -> None:
        """Run the session's startup sequence on a connected client.

        The sequence itself lives in ``session.py``, which imports no Home
        Assistant, so that it can also be driven from a terminal against real
        hardware (``tools/dump_bus.py --live``).
        What is left here is the translation into Home Assistant's own
        exception, so that the session loop keeps seeing one kind of failure.
        """
        try:
            await session.run_startup(
                client, self._bus, self._identity, self.unique_id, self.hass.loop.time
            )
        except session.StartupFailed as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _request_measurements(self, client: TrumaBleClient) -> None:
        """Ask the on-demand sensors for a fresh reading; see session.py."""
        await session.request_measurements(client, self._bus, self.unique_id)

    @callback
    def _on_frame(self, parsed: dict) -> None:
        """Handle a decoded V3 frame and update state.

        The decoding itself is in ``session.handle_frame``, which imports no
        Home Assistant, so the same frames can be filed into a bus from a
        terminal. What is left here is the two things only Home Assistant
        cares about: the stall watchdog, and telling the entities.
        """
        # Any frame proves the link is alive; feed the stall watchdog.
        self._last_frame = self.hass.loop.time()

        if session.handle_frame(self._bus, parsed, self._client, self.unique_id):
            self.async_set_updated_data(self._bus)

    @callback
    def _mark_disconnected(self) -> None:
        """Flag the link as down and notify entities."""
        if self._bus.connected:
            self._bus.connected = False
            self.async_set_updated_data(self._bus)

    async def _client_for_write(self) -> TrumaBleClient:
        """A connected client to write through, waking a poll if need be.

        In connected mode there is always a live link. In poll mode there
        usually is not: hanging up between readings is the point. Rather than
        refuse the command -- which is what a button press got, "Truma panel is
        not connected" -- ask the loop for a session now and wait for it. The
        poll will not hang up while the write is outstanding.
        """
        client = self._client
        if client is not None and client.connected:
            return client
        if not self.poll_interval:
            raise HomeAssistantError("Truma panel is not connected")

        LOGGER.debug("Truma %s: write requested; waking a poll", self.unique_id)
        self._wake_event.set()
        try:
            await asyncio.wait_for(
                self._connected_event.wait(), timeout=_WRITE_CONNECT_TIMEOUT
            )
        except TimeoutError:
            raise HomeAssistantError(
                "Truma panel did not answer in time for the command"
            ) from None
        client = self._client
        if client is None or not client.connected:
            raise HomeAssistantError("Truma panel is not connected")
        return client

    async def async_write(
        self, addr: int, topic: str, param: str, value: int
    ) -> None:
        """Validate and send a parameter write to the device that owns it.

        ``addr`` is the bus address of the entity's own device, which is where
        the write goes -- the sole exception being the handful of topics the
        panel relays for the bus (see ``COMMAND_DEST``). Addressing a write to
        a device named in the source used to be the bug class of #10: a write
        of AirCooling.TgtTemp to the Combi was acknowledged by the transport
        and then silently dropped, because on that vehicle the thing that
        cools is a roof unit at its own address.

        The device confirms by pushing an updated value, which flows back
        through the normal notification path and updates the entity.
        """
        ok, msg = self._bus.validate_write(addr, topic, param, value)
        if not ok:
            raise HomeAssistantError(f"Invalid Truma command: {msg}")

        # Held across the whole write, not just the wait for a link: in poll
        # mode the loop checks this before hanging up, and releasing it early
        # would let it disconnect between getting the client and sending.
        self._writes_pending += 1
        try:
            client = await self._client_for_write()

            dest = self._bus.command_dest(addr, topic)
            frame = build_write_frame(client.assigned_addr, dest, topic, param, value)
            LOGGER.debug(
                "Truma write %s.%s = %s -> 0x%04X", topic, param, value, dest
            )
            if not await client.send(frame):
                raise HomeAssistantError(
                    f"Truma did not acknowledge write {topic}.{param}={value}"
                )
        finally:
            self._writes_pending -= 1
