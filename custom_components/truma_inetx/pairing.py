"""Bonding (pairing) for the Truma iNet X panel.

The panel uses **Just Works** pairing (no passkey shown) and only bonds while a
client is *actively* attempting to pair AND the panel is in add-device mode. It
also silently rejects new bonds when its stored device list is full, so the user
must clear that list first if pairing fails repeatedly.

``ensure_bonded()`` hides that behind one call. It connects first and then
bonds the way the link it was given requires — the transport is Home
Assistant's choice, so asking the connected client beats guessing from what
can hear the panel:

* **Over a proxy link** (``_bond_over_link``) — ``pair()`` with bleak, then
  verify the bond by accessing a protected characteristic. Validated
  end-to-end against the real panel. Re-pairing needs no clean-up on the
  proxy and so works on stock proxy firmware — see the ``avoid`` rotation in
  ``ensure_bonded``.
* **Over a local adapter** (``_ensure_bonded_bluez``) — bleak cannot do this
  one: BlueZ needs an agent registered to answer the Just Works confirmation.
  A faithful port of ``scripts/ha_pair.py``: register a NoInputNoOutput
  auto-accept agent, then busy-loop ``Device1.Pair()`` until the device
  reports ``Paired``. Not yet validated end-to-end from inside HA against a
  capable adapter.

Bonding where HA will *connect* is the whole point of the order: a bond that
lives on a path HA does not use fails every later connect at encryption, which
looks exactly like broken hardware.
"""

from __future__ import annotations

import asyncio
import time

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from homeassistant.core import HomeAssistant

from .ble import client_is_proxy
from .bt import async_resolve_device
from .const import LOGGER
from .truma.const import CHAR_CMD

BLUEZ = "org.bluez"
_AGENT_PATH = "/truma_inetx/agent"
_PAIR_CALL_TIMEOUT = 8.0
_POLL_INTERVAL = 1.0


# --- bonding -----------------------------------------------------------------


async def ensure_bonded(
    hass: HomeAssistant,
    name: str,
    address: str,
    *,
    adapter_path: str | None = None,
    timeout: float = 60.0,
) -> tuple[bool, BleakClientWithServiceCache | None]:
    """Ensure the Truma panel is BLE-bonded, over whatever transport HA gives us.

    Connect first, then bond the way the link in hand requires. The transport
    is not ours to choose -- Home Assistant scores every connectable path and
    re-picks at each connect -- and it is not ours to *guess* either: this used
    to probe for a proxy and take the proxy path whenever one could hear the
    panel, which bonded the panel to the proxy on hosts whose sessions then ran
    over the local adapter, where no bond exists. A bond that is not on the path
    HA connects through is worse than no bond: every later connect establishes
    and then fails to encrypt.

    The caller must have prompted the user to put the panel into add-device
    mode (and to clear its device list if it is full).

    Returns ``(bonded, client)``. Over a proxy link ``client`` is the LIVE,
    encrypted connection left open for the coordinator to adopt (handing it off
    avoids the disconnect/reconnect that wedges the just-bonded RPA); the caller
    owns it and must disconnect it if it does not hand it off. Over local BlueZ
    (and on failure) ``client`` is ``None``. Safe to call when already bonded.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    # The panel advertises a post-pairing PHANTOM RPA alongside the live one:
    # same name, both reachable, near-identical timestamps, but the phantom
    # never completes a bond (0x3e on connect, or error 97 / "insufficient
    # authentication" on the protected write). The resolver returns the freshest
    # first, so without feedback we'd re-pick and hammer the phantom until
    # timeout. Track addresses that failed so the resolver demotes them and we
    # rotate to the live RPA -- the same avoid-rotation the coordinator uses for
    # reconnect. Demotion, not exclusion: when the failed address is all the
    # panel is advertising it comes back and we retry it, which is right,
    # because a bond can also fail for reasons that heal.
    avoid: set[str] = set()
    while time.monotonic() < deadline:
        device = async_resolve_device(hass, name, avoid=avoid)
        if device is None:
            # Nothing is on air yet. Any address we failed on is one the panel
            # has since rotated away from, so forget them rather than carrying
            # grudges into the next advert. (A candidate that is still on air
            # is never withheld -- avoid only demotes, see
            # bt.async_resolve_device -- so this cannot mean "all banished".)
            avoid.clear()
            await asyncio.sleep(1.5)
            continue
        try:
            client = await establish_connection(
                BleakClientWithServiceCache, device, device.address, max_attempts=1
            )
        except Exception as exc:  # noqa: BLE001 - transient connect failures
            last_exc = exc
            LOGGER.debug("Truma %s pairing connect: %s", name, exc)
            avoid.add(device.address.upper())
            await asyncio.sleep(2.0)
            continue

        if not client_is_proxy(client):
            # HA put us on the host's own adapter. Bonding there is BlueZ's job
            # and cannot be done through bleak: it needs an agent registered to
            # answer the Just Works confirmation, and Device1.Pair() on the
            # BlueZ object. Drop the link and hand the rest over.
            LOGGER.debug(
                "Truma %s: Home Assistant connected over a local adapter; "
                "bonding through BlueZ",
                name,
            )
            await _drop(client, name)
            bonded = await _ensure_bonded_bluez(
                name,
                address,
                adapter_path=adapter_path,
                timeout=max(deadline - time.monotonic(), 0.0),
                hass=hass,
            )
            return bonded, None

        if await _bond_over_link(name, client):
            # Hand the live, encrypted connection back to the caller -- do NOT
            # disconnect. Reusing it for the session avoids the reconnect that
            # wedges the just-bonded RPA.
            return True, client
        # This address didn't bond -- drop the client, demote the address, and
        # let the resolver hand us the panel's other RPA if it has one.
        await _drop(client, name)
        avoid.add(device.address.upper())
        await asyncio.sleep(2.0)
    LOGGER.warning("Truma %s: pairing timed out (%s)", name, last_exc)
    return False, None


async def _drop(client: BleakClientWithServiceCache, name: str) -> None:
    """Close a link we are not going to hand off (best effort)."""
    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.debug("Truma %s pairing disconnect: %s", name, exc)


def _noop_notify(_sender: object, _data: bytearray) -> None:
    """Discard notifications during the pairing bond test."""


async def _bond_over_link(name: str, client: BleakClientWithServiceCache) -> bool:
    """Bond on a live proxy-carried link: ``pair()``, then prove it took.

    An ESPHome proxy encrypts lazily, so pair()/encrypt first, then confirm the
    bond by subscribing to a protected characteristic -- a CCCD write only
    succeeds on an encrypted link. Retries briefly to absorb the
    encryption-setup delay.
    """
    for _ in range(3):
        try:
            await client.pair()
        except Exception as exc:  # noqa: BLE001 - not all paths need it
            # "error: 97" here means the panel has forgotten a bond the proxy
            # still holds (its device list was cleared, or rolled our entry out
            # of its ~4 slots). Nothing to do about it on this address -- the
            # panel drops the link the instant it rejects the bond. The
            # avoid-rotation in ensure_bonded is the cure: the panel's next RPA
            # is one the proxy holds no bond for, so pairing there is clean.
            # Measured twice on the van (2026-07-26): rejected, rotated,
            # bonded, ~9s total.
            LOGGER.debug("Truma %s pair(): %s", name, exc)
        try:
            # A protected CCCD write only lands on an encrypted (bonded) link --
            # success here means the bond took.
            await client.start_notify(CHAR_CMD, _noop_notify)
            await client.stop_notify(CHAR_CMD)
            LOGGER.info("Truma %s bonded", name)
            return True
        except Exception as exc:  # noqa: BLE001 - retry through encrypt race
            LOGGER.debug("Truma %s bond check: %s", name, exc)
            await asyncio.sleep(1.5)
    return False


# --- local BlueZ pairing (D-Bus) — when HA connects over a host adapter -----


class _JustWorksAgent(ServiceInterface):
    """A BlueZ agent that auto-accepts everything (Just Works, no passkey)."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):  # noqa: N802
        """Agent released by BlueZ."""

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        """Return a dummy PIN (not used by Just Works)."""
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        """No display."""

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        """Return a dummy passkey (not used by Just Works)."""
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        """No display."""

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        """Auto-confirm (no exception raised == accept)."""

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        """Auto-authorize."""

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        """Auto-authorize the service."""

    @method()
    def Cancel(self):  # noqa: N802
        """Pairing cancelled by BlueZ."""


async def _get_interface(bus: MessageBus, path: str, interface: str):
    """Return a proxy interface at ``path``."""
    introspection = await bus.introspect(BLUEZ, path)
    obj = bus.get_proxy_object(BLUEZ, path, introspection)
    return obj.get_interface(interface)


def _find_device(
    objects: dict, *, name: str, address: str, adapter_path: str | None = None
) -> str | None:
    """Return the BlueZ device path matching ``name`` (or ``address``).

    When ``adapter_path`` is given, only devices under that adapter are
    considered, so the bond lands on the adapter HA connects through rather
    than any adapter that happens to see the panel.
    """
    address = address.upper()
    name_lc = name.lower()
    for path, ifaces in objects.items():
        if adapter_path and not path.startswith(f"{adapter_path}/"):
            continue
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        dev_addr = dev.get("Address")
        dev_name = dev.get("Name")
        dev_addr_v = dev_addr.value.upper() if dev_addr else ""
        dev_name_v = str(dev_name.value) if dev_name else ""
        if dev_addr_v == address or (name_lc and name_lc in dev_name_v.lower()):
            return path
    return None


def _already_bonded(
    objects: dict, *, path: str | None, adapter_path: str | None
) -> bool:
    """Whether an existing bond may be trusted as "this panel, this adapter".

    Requires knowing which adapter the bond must be on. Without that scope,
    ``_find_device`` matches a bond on ANY adapter BlueZ knows -- including one
    whose Home Assistant config entry is disabled but which is still powered and
    still holds the old bond. That made pairing report success in milliseconds
    while the panel sat in add-device mode having seen nothing (observed on the
    van, 2026-08-23, with the USB dongle disabled and its Truma bond intact).

    Re-pairing an already-bonded panel is cheap and visible to the user;
    falsely reporting success is neither. So when the adapter is unknown, say
    no and let the caller actually pair.
    """
    if not adapter_path or not path:
        return False
    return _is_paired(objects, path)


def _is_paired(objects: dict, path: str) -> bool:
    """Whether the device at ``path`` reports ``Paired``."""
    dev = objects.get(path, {}).get("org.bluez.Device1", {})
    paired = dev.get("Paired")
    return bool(paired and paired.value)


def _live_device_path(
    hass: HomeAssistant | None, name: str, adapter_path: str | None
) -> str | None:
    """BlueZ object path of the panel's *current* advertised address.

    ``_find_device`` matches on the identity address or the local name, and in
    add-device mode the panel offers neither: it advertises a rotating RPA with
    no name, so BlueZ knows it as e.g. ``dev_49_3E_CD_8E_2F_8B``. Scoped to the
    pairing adapter, that search finds nothing and the loop below never calls
    ``Pair()`` at all (observed on the van, 2026-08-23: agent registered, sixty
    seconds of silence, timeout).

    Ask the resolver instead, every iteration, so a rotation mid-pairing moves us
    to the new address rather than stranding us on a dead one.

    ``local_only``: only a local adapter's device carries the BlueZ object path
    this returns. Without it the resolver may hand back the proxy's view of the
    same address, which has no path, and the caller then behaves as if BlueZ
    had never heard of a panel it can plainly see.
    """
    if hass is None:
        return None
    device = async_resolve_device(hass, name, local_only=True)
    details = getattr(device, "details", None)
    if not isinstance(details, dict):
        return None
    path = details.get("path")
    if not isinstance(path, str) or not path.startswith("/org/bluez/"):
        return None
    if adapter_path and not path.startswith(f"{adapter_path}/"):
        return None
    return path


async def _ensure_bonded_bluez(
    name: str,
    address: str,
    *,
    adapter_path: str | None = None,
    timeout: float = 60.0,
    hass: HomeAssistant | None = None,
) -> bool:
    """Bond the Truma panel over local BlueZ (D-Bus). Return ``True`` if bonded.

    Registers a temporary Just Works agent and busy-loops ``Device1.Pair()``
    until the panel reports ``Paired`` or ``timeout`` elapses. The caller must
    have prompted the user to put the panel into add-device mode (and to clear
    its device list if it is full).

    ``adapter_path`` (e.g. ``/org/bluez/hci0``) scopes the bond to the adapter
    HA connects through; when omitted, any adapter that sees the panel is used.

    BlueZ transport only. Safe to call when already bonded (returns quickly).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = _JustWorksAgent()
    registered = False
    try:
        object_manager = await _get_interface(
            bus, "/", "org.freedesktop.DBus.ObjectManager"
        )

        # Fast path: already bonded (on the connecting adapter)?
        objects = await object_manager.call_get_managed_objects()
        path = _find_device(
            objects, name=name, address=address, adapter_path=adapter_path
        )
        if _already_bonded(objects, path=path, adapter_path=adapter_path):
            LOGGER.debug("Truma %s already bonded on %s", name, adapter_path)
            return True
        if path and not adapter_path:
            LOGGER.debug(
                "Truma %s: no adapter scope, pairing %s rather than trusting "
                "an existing bond",
                name,
                path,
            )

        # Register our auto-accept agent as the default for the pairing window.
        bus.export(_AGENT_PATH, agent)
        agent_manager = await _get_interface(
            bus, "/org/bluez", "org.bluez.AgentManager1"
        )
        await agent_manager.call_register_agent(_AGENT_PATH, "NoInputNoOutput")
        await agent_manager.call_request_default_agent(_AGENT_PATH)
        registered = True

        LOGGER.info("Truma %s: attempting Just Works bond (%ss)", name, timeout)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            objects = await object_manager.call_get_managed_objects()
            path = _live_device_path(hass, name, adapter_path) or _find_device(
                objects, name=name, address=address, adapter_path=adapter_path
            )
            if path and _is_paired(objects, path):
                LOGGER.info("Truma %s bonded", name)
                return True
            if path:
                await _try_pair(bus, path)
            else:
                LOGGER.debug("Truma %s: no device object to pair yet", name)
            await asyncio.sleep(_POLL_INTERVAL)

        LOGGER.warning("Truma %s: pairing timed out after %ss", name, timeout)
        return False
    finally:
        if registered:
            try:
                await agent_manager.call_unregister_agent(_AGENT_PATH)
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Truma agent unregister failed: %s", exc)
        bus.disconnect()


async def _try_pair(bus: MessageBus, path: str) -> None:
    """One pairing attempt against the device at ``path`` (best effort)."""
    device = await _get_interface(bus, path, "org.bluez.Device1")
    properties = await _get_interface(
        bus, path, "org.freedesktop.DBus.Properties"
    )
    try:
        await properties.call_set(
            "org.bluez.Device1", "Trusted", Variant("b", True)
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Truma set-trusted failed: %s", exc)
    try:
        await asyncio.wait_for(device.call_pair(), timeout=_PAIR_CALL_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - expected until the panel accepts
        LOGGER.debug("Truma pair attempt: %s", str(exc)[:80])
