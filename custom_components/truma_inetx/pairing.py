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
  reports ``Paired``, and drop the link that pairing opened (see
  ``_release_link``, without which nothing can connect afterwards).
  Validated end-to-end on the van, 2026-09-18: bonded in 6 s.

Bonding where HA will *connect* is the whole point of the order: a bond that
lives on a path HA does not use fails every later connect at encryption, which
looks exactly like broken hardware.

That order has one hole, and it is the whole of #26: on a host with no proxy,
the connect it dispatches on is the thing bonding is *for*. An unbonded panel
drops the link before its services resolve, so ``establish_connection`` never
returns a client, the dispatch never runs, and ``Device1.Pair()`` is never
called -- twenty-six attempts, sixty seconds, the agent never registered
(measured on a Raspi 3B with a USB dongle, 2026-09-17). So a connect that has
failed on every address the panel is advertising falls back to the BlueZ path,
which needs no link of its own in order to bond. Only the fallback is new: a
connect that *succeeds* still decides the transport, because a link in hand is
the only thing that ever predicted it.

A *re-pair* of an entry that already exists takes the opposite order, on purpose
(``own_the_link``): the panel is known to answer links, so the link Home
Assistant dials is kept, the Just Works exchange runs over it, and that same
link goes on to carry the session. Bonding through BlueZ while a session holds a
link meant two diallers for one panel, and on 2026-10-25 that ended with the
panel refusing our key outright -- 47 x HCI 0x0E in six minutes -- after sixty
seconds of re-asking. If a re-pair cannot dial at all, that is #26's shape again
and the BlueZ path is still taken, once, with the budget that is left.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from homeassistant.core import HomeAssistant

from .ble import client_is_proxy
from .bt import async_has_proxy_route, async_resolve_device
from .const import LOGGER, has_truma_uuid
from .truma.const import CHAR_CMD

BLUEZ = "org.bluez"
_AGENT_PATH = "/truma_inetx/agent"
_PAIR_CALL_TIMEOUT = 8.0
_POLL_INTERVAL = 1.0
# What the BlueZ path is guaranteed, however the connect rotation spends
# itself. Measured on the van (2026-09-18): two candidate addresses at the
# ~20 s bleak takes to give up on each left the hand-over 15 s of a 60 s
# budget and it timed out; the same bond took 3.2 s when it had the room.
_BLUEZ_RESERVE = 25.0
# How long a Pair() call BlueZ is still working on is left alone. The call
# itself is abandoned at _PAIR_CALL_TIMEOUT, but the daemon carries on --
# and answers every further call with InProgress, which is not a refusal
# and must not be retried as one (measured on the van, 2026-09-18: two
# InProgress answers were the whole of a 15 s window).
_PAIR_PENDING_WAIT = 12.0
# How long a pairing BlueZ keeps answering InProgress is left to run before it
# is cancelled and re-issued. A bond that is going to take does so in seconds
# (3.2 s measured on the van); InProgress past this means the daemon is dialling
# an address nothing answers on -- which is what the identity address becomes
# the moment the host's bond, and with it the panel's IRK, is dropped. Waiting
# it out spends the whole budget on a call that cannot finish (measured on the
# van, 2026-09-18: 51 s of InProgress, not one connection attempt on air).
_PAIR_PENDING_LIMIT = 20.0
# How long the link Pair() opened is given to go away. Best effort: a
# disconnect that hangs must not hold up a bond that already succeeded.
_DISCONNECT_TIMEOUT = 5.0
# How long the consent-gated fallback may spend bonding through BlueZ when a
# re-pair could not dial at all. Deliberately short: the fallback is the
# ambiguous path, and a panel that is refusing us is made worse by asking it
# again (measured on the van 2026-10-25).
_FALLBACK_BOND = 20.0
# What one more ask on the same link can cost. The proxy client waits 30 s for
# a pairing response (measured on the van 2026-10-28), so a retry that cannot
# fit in what is left is not started: the caller needs its remaining budget to
# rotate, and the rotation is the only cure for a refused bond. Costs at most
# one attempt -- never a bond.
_RETRY_NEEDS = 31.0
# How long an address that failed to bond is left alone before it is dialled
# again inside one attempt. A rejection is not transient, and re-asking a
# refusing panel is what has cost this panel its key before (measured on the
# van 2026-10-25: 47 x HCI 0x0E in six minutes). At the 60 s budget this still
# allows three asks, which is what the address that heals after a panel power
# cycle needs.
_FAILED_DIAL_SPACING = 20.0
# What a rejected bond looks like on the wire, in the words this stack uses.
# Not a general classifier: the one distinction the retry loop needs is "the
# panel said no to this address" (final -- rotate) versus "the exchange did not
# finish" (worth another ask on the same link).
_REFUSAL_MARKERS = (
    "error: 97",
    "error: 0x61",  # the same SMP failure, spelled in hex
    "error: 0x0e",  # HCI "rejected for security reasons", panel side
    "authenticationfailed",
    "authentication failed",
    "authentication rejected",
    "insufficient authentication",
    "not permitted",
)


# --- bonding -----------------------------------------------------------------


async def ensure_bonded(
    hass: HomeAssistant,
    name: str,
    address: str,
    *,
    adapter_path: str | None = None,
    timeout: float = 60.0,
    may_forget_bond: bool = False,
    own_the_link: bool = False,
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

    When no address the panel is advertising will even establish a link, there
    is no client to dispatch on and the loop above has nothing left to try:
    that is #26, where a proxyless host spent the whole timeout re-dialling a
    panel that drops every unbonded link. A connect failure on an address we
    have already failed to connect on means the rotation has wrapped, so the
    BlueZ path is taken anyway -- it bonds over its own D-Bus connection and
    needs no link from us. Gated on the panel resolving to a BlueZ object path,
    because that path is what ``Device1.Pair()`` is called on: without one there
    is nothing for the fallback to do, and the failure is somewhere else.

    Returns ``(bonded, client)``. Over a proxy link ``client`` is the LIVE,
    encrypted connection left open for the coordinator to adopt (handing it off
    avoids the disconnect/reconnect that wedges the just-bonded RPA); the caller
    owns it and must disconnect it if it does not hand it off. Over local BlueZ
    (and on failure) ``client`` is ``None``. Safe to call when already bonded.

    ``own_the_link`` is for the caller that knows a bond already exists -- a
    Reconfigure of a configured entry. It inverts the ordering below: rather
    than bonding through BlueZ *before* dialling, the link Home Assistant dials
    is kept, the Just Works exchange runs over it, and that same link is handed
    back for the session. The difference is not cosmetic. `Device1.Pair()` dials
    the panel itself, so bonding through BlueZ while a session holds a link
    means two diallers for one panel: ours is dropped, BlueZ keeps its own
    afterwards (``Connected: no``, an ACL nothing in Home Assistant can close),
    and the panel -- measured on the van 2026-10-25 -- stops honouring our key
    after 60 s of repeated requests. One dialler, one link, both ours.
    """
    deadline = time.monotonic() + timeout
    # Bond before dialling where dialling cannot work. A panel with no bond
    # drops every link it is offered, so on a host whose own adapter can see
    # it there is nothing for a connect to learn and a whole budget for it to
    # spend: this used to reach the BlueZ path with nothing left (#26), and
    # even with the hand-over in place two candidate addresses left it 15 s of
    # 60. A proxyless host bonds where it will connect, which is what 0.7.1b5
    # did before connect-first, and what c91f711 was never arguing against --
    # its case is the host that has *both*, where a local bond can land on a
    # path nothing uses. That host still dials first.
    if (
        not own_the_link
        and not async_has_proxy_route(hass, name)
        and _live_device_path(hass, name, adapter_path)
    ):
        LOGGER.debug(
            "Truma %s: no proxy route, so bonding through BlueZ before dialling",
            name,
        )
        if await _ensure_bonded_bluez(
            name,
            address,
            adapter_path=adapter_path,
            timeout=max(deadline - time.monotonic(), 0.0),
            hass=hass,
            # Nothing has been proven about the panel here -- that is the
            # point of arriving before the first dial -- so a bond BlueZ
            # reports is exactly the half-held one this path exists to
            # replace. Measured on the van (2026-09-18): a record holding
            # only signature keys and no LongTermKey still reads back as
            # Paired *and* Bonded, pairing returned success in under a
            # millisecond with not one SMP frame on air, and the panel sat
            # in add-device mode having seen nothing while every later
            # connect timed out.
            trust_existing_bond=False,
            may_forget_bond=may_forget_bond,
        ):
            return True, None
        # Not bonded. Fall through rather than give up: the panel may yet
        # answer a link, and the rotation below is what finds the address it
        # answers on.
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
    # Addresses that would not even establish a link, as opposed to ``avoid``,
    # which also collects addresses that connected and then failed to bond.
    # The fallback below turns on the difference: a bond failure has a client
    # behind it and is the rotation's business, while a connect failure on
    # every candidate means no transport was ever chosen at all (#26).
    connect_failed: set[str] = set()
    # Never more than half the budget: the reserve is there so a slow rotation
    # cannot leave the bond nothing, not so a short timeout skips dialling.
    reserve = min(_BLUEZ_RESERVE, timeout / 2)
    # Whether any address gave us a link at all. A re-pair that cannot dial is a
    # different failure from one that dials and is refused: the first is #26's
    # shape (a panel with no bond drops every link), where bonding through BlueZ
    # is the only way in and must still be tried; the second is a panel that is
    # reachable and refusing, where a second dialler is exactly what to avoid.
    dialled = False
    # Consent, and spent at most once. Dropping the host's record is
    # destructive and irreversible, so it may only ever happen because the user
    # ticked the box -- and then only against a refusal the panel has already
    # given, never speculatively.
    forgot_bond = False
    # Addresses that were dialled and did not bond, and when each may be asked
    # again. See _FAILED_DIAL_SPACING: a refusal must not turn into a tight
    # re-dial loop, now that a refusal no longer blocks the loop for 60 s.
    failed_dial: dict[str, float] = {}
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
        address = device.address.upper()
        wrapped = device.address.upper() in connect_failed
        # Only once dialling has actually failed at something. A link that
        # establishes and then refuses the bond is the rotation's case (the
        # error-97 path), and handing that to a local bond is the c91f711
        # mistake; the reserve is for a panel that answers nothing.
        starved = bool(connect_failed) and time.monotonic() >= deadline - reserve
        if (
            (wrapped or starved)
            # Not for a re-pair: there the second dialler is the thing being
            # avoided, and the decision is taken once, at the end, under the
            # user's consent (see the timeout branch below).
            and not own_the_link
            and _live_device_path(hass, name, adapter_path)
        ):
            # Either the resolver has handed back an address we already
            # failed to connect on -- every candidate has had its turn and the
            # rotation has wrapped with nothing to show -- or the connect phase
            # has spent everything it may and the reserve is all that is left.
            # Since BlueZ can see the panel, bond there: Device1.Pair() brings
            # up its own link and does the SMP exchange that the plain connect
            # was waiting for the panel to volunteer.
            LOGGER.debug(
                "Truma %s: %s; bonding through BlueZ instead of re-dialling",
                name,
                "no address will establish a link"
                if wrapped
                else "the connect rotation has used its share of the timeout",
            )
            bonded = await _ensure_bonded_bluez(
                name,
                address,
                adapter_path=adapter_path,
                timeout=max(deadline - time.monotonic(), 0.0),
                hass=hass,
                trust_existing_bond=False,
                may_forget_bond=may_forget_bond,
            )
            return bonded, None

        # A failed address is spaced, not stacked. A fresh address is never
        # affected, so the rotation stays immediate -- which is the whole point
        # of it, and the reason this gate sits here, after the handover above
        # has had its chance and before this address is dialled again.
        wait = failed_dial.get(address, 0.0) - time.monotonic()
        if wait > 0:
            if time.monotonic() + wait >= deadline:
                LOGGER.debug(
                    "Truma %s: %s did not bond and there is no budget left to "
                    "ask it again",
                    name,
                    address,
                )
                return False, None
            await asyncio.sleep(wait)
            continue
        try:
            client = await establish_connection(
                BleakClientWithServiceCache, device, device.address, max_attempts=1
            )
        except Exception as exc:  # noqa: BLE001 - transient connect failures
            last_exc = exc
            LOGGER.debug("Truma %s pairing connect: %s", name, exc)
            avoid.add(device.address.upper())
            connect_failed.add(device.address.upper())
            await asyncio.sleep(2.0)
            continue
        dialled = True

        if not client_is_proxy(client):
            if own_the_link:
                # A re-pair: this link is already up, so the exchange runs over
                # it and the same link goes on to carry the session. Nothing
                # else touches the panel, which is the point -- Device1.Pair()
                # would dial it a second time, keep that link afterwards, and
                # leave the panel refusing us (measured on the van 2026-10-25).
                bonded, refused = await _bond_over_local_link(
                    name, client, deadline=deadline
                )
                if bonded:
                    return True, client
                # Refused. Let the link go and report failure: falling through
                # to the Pair() loop is what turned one refusal into 47 x 0x0E.
                LOGGER.debug(
                    "Truma %s: the panel refused a bond over the link we "
                    "dialled; not escalating to Device1.Pair()",
                    name,
                )
                # ...but a record that *looks* complete while the panel no
                # longer knows it cannot be repaired by trying again. Measured
                # on the van 2026-10-27 (panel armed, its device list cleared,
                # this host holding a full record): 20 connects put **zero** SMP
                # frames on air, because BlueZ tried to encrypt with the stale
                # LTK and the panel answered HCI 0x0E -- `bluetoothd`:
                # `device_bonding_complete() status 0x0e`. With that record
                # removed by hand, the same press bonded in **9** SMP frames.
                # So when the user has ticked "forget the stale bond", spend
                # that consent here, once: drop the host's half, redial, and let
                # the exchange run against a record that no longer lies.
                if (
                    may_forget_bond
                    and not forgot_bond
                    and (path := _live_device_path(hass, name, adapter_path))
                ):
                    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                    try:
                        forgot_bond = await _forget(
                            bus, path, name, adapter_path=adapter_path
                        )
                    finally:
                        bus.disconnect()
                    if forgot_bond:
                        await _drop(client, name)
                        # Not `avoid`: the address is fine, its record was not.
                        avoid.discard(device.address.upper())
                        await asyncio.sleep(1.0)
                        continue
                await _drop(client, name)
                avoid.add(address)
                if refused:
                    failed_dial[address] = time.monotonic() + _FAILED_DIAL_SPACING
                await asyncio.sleep(2.0)
                continue
            # First pairing, or a host that cannot dial: BlueZ holds the bond.
            # HA put us on the host's own adapter.
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

        bonded, refused = await _bond_over_link(name, client, deadline=deadline)
        if bonded:
            # Hand the live, encrypted connection back to the caller -- do NOT
            # disconnect. Reusing it for the session avoids the reconnect that
            # wedges the just-bonded RPA.
            return True, client
        # This address didn't bond -- drop the client, demote the address, and
        # let the resolver hand us the panel's other RPA if it has one.
        await _drop(client, name)
        avoid.add(address)
        if refused:
            # The address itself is in question (the panel said no, or never
            # answered): let it rest, so a refusal cannot turn into a re-dial
            # loop now that it no longer spends the budget by itself.
            failed_dial[address] = time.monotonic() + _FAILED_DIAL_SPACING
        await asyncio.sleep(2.0)
    LOGGER.warning("Truma %s: pairing timed out (%s)", name, last_exc)
    # A re-pair that never got a link is ambiguous, and the two readings have
    # opposite cures: a panel whose device list was just cleared drops every
    # link because it holds no bond (bonding through BlueZ is then the way in),
    # while a panel that has kept its list and dropped *our key* refuses every
    # link with HCI 0x0E -- and there the second dialler is what makes it worse.
    # Measured the hard way on the van 2026-10-25: this fallback ran unasked on
    # the second case, spent 38 s in Pair(), and left the panel refusing our
    # key with an ACL in BT_CONFIG behind it.
    #
    # Nothing on this host can tell the two apart, so the user does: forgetting
    # a bond is the destructive act, and ticking that box is consent to the path
    # that needs it. Bounded, because a consent given for one attempt must not
    # turn into an unbounded storm.
    if (
        own_the_link
        and not dialled
        and may_forget_bond
        and _live_device_path(hass, name, adapter_path)
    ):
        LOGGER.debug(
            "Truma %s: no link could be dialled and the bond may be forgotten, "
            "so bonding through BlueZ after all",
            name,
        )
        return (
            await _ensure_bonded_bluez(
                name,
                address,
                adapter_path=adapter_path,
                timeout=min(max(deadline - time.monotonic(), 0.0), _FALLBACK_BOND),
                hass=hass,
                trust_existing_bond=False,
                may_forget_bond=may_forget_bond,
            ),
            None,
        )
    return False, None


async def _drop(client: BleakClientWithServiceCache, name: str) -> None:
    """Close a link we are not going to hand off (best effort)."""
    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.debug("Truma %s pairing disconnect: %s", name, exc)


def _noop_notify(_sender: object, _data: bytearray) -> None:
    """Discard notifications during the pairing bond test."""


async def _bond_over_link(
    name: str,
    client: BleakClientWithServiceCache,
    *,
    deadline: float | None = None,
) -> tuple[bool, bool]:
    """Bond on a live proxy-carried link: ``pair()``, then prove it took.

    An ESPHome proxy encrypts lazily, so pair()/encrypt first, then confirm the
    bond by subscribing to a protected characteristic -- a CCCD write only
    succeeds on an encrypted link. Retries briefly to absorb the
    encryption-setup delay.

    Returns ``(bonded, refused)``. ``refused`` means the *address* is in
    question -- the panel rejected the bond, or was asked and never answered --
    so the caller must let it rest rather than dial it straight back. A retry
    is only for the encryption race; it is never an answer to a bond the panel
    has already said no to.

    ⚠️ **The retry is bounded by what the transport charges, not by a count.**
    Measured on the van 2026-10-28 13:52 (0.9.1b1, owner: *"I had to press
    submit twice"*): the first ``pair()`` on the proxy link answered ``Pairing
    failed due to error: 97`` in 134 ms, and the two calls after it on the same
    link each waited out the ESPHome proxy's own **30 s** pairing timeout -- so
    three asks were the whole 60 s ``_PAIR_TIMEOUT``. ``ensure_bonded`` then had
    no iteration left to rotate onto the address the panel had moved to (it
    first advertised it 6 s into the attempt), reported failure, and bonded in
    2.5 s on the next Submit. The rotation is this module's documented cure for
    error 97, so this loop may not spend the budget it needs.
    """
    pair_raised = False
    for attempt in range(3):
        if (
            attempt
            and deadline is not None
            and time.monotonic() + _RETRY_NEEDS > deadline
        ):
            LOGGER.debug(
                "Truma %s: only %.0fs of budget left and a pairing call may "
                "take two of them; not re-asking this address, leaving the "
                "rest to the rotation",
                name,
                max(deadline - time.monotonic(), 0.0),
            )
            return False, True
        refused = False
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
            # bonded, ~9s total. So a refusal ends this address now, while
            # there is still budget to rotate with.
            LOGGER.debug("Truma %s pair(): %s", name, exc)
            pair_raised = True
            refused = _is_refusal(exc)
        try:
            # A protected CCCD write only lands on an encrypted (bonded) link --
            # success here means the bond took.
            await client.start_notify(CHAR_CMD, _noop_notify)
            await client.stop_notify(CHAR_CMD)
            LOGGER.info("Truma %s bonded", name)
            return True, False
        except Exception as exc:  # noqa: BLE001 - retry through encrypt race
            LOGGER.debug("Truma %s bond check: %s", name, exc)
            if refused:
                return False, True
            await asyncio.sleep(1.5)
    return False, pair_raised


def _is_refusal(exc: BaseException) -> bool:
    """Whether ``pair()`` failed because the panel refused the bond.

    The distinction the loop needs and nothing more: a refusal is final for the
    address (the cure is a different one), while a call that timed out or
    answered "already in progress" says nothing about the address and is worth
    another ask on the same link.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _REFUSAL_MARKERS)


# --- local BlueZ pairing (D-Bus) — when HA connects over a host adapter -----


@asynccontextmanager
async def _agent_registered(bus: MessageBus):
    """Offer the auto-accept agent to BlueZ for the duration of the block.

    ``client.pair()`` is ``Device1.Pair()`` on the BlueZ backend, and that needs
    an agent to answer the Just Works confirmation. It is registered here rather
    than left in place for the life of the process: a default agent that answers
    everything would also confirm pairing requests nothing in this integration
    asked for.

    Yields whether the agent is registered. A failure is not fatal on its own --
    the caller finds out when the exchange has no one to confirm it.
    """
    agent = _JustWorksAgent()
    registered = False
    agent_manager = None
    bus.export(_AGENT_PATH, agent)
    try:
        agent_manager = await _get_interface(
            bus, "/org/bluez", "org.bluez.AgentManager1"
        )
        await agent_manager.call_register_agent(_AGENT_PATH, "NoInputNoOutput")
        registered = True
        await agent_manager.call_request_default_agent(_AGENT_PATH)
    except Exception as exc:  # noqa: BLE001 - best effort, reported below
        LOGGER.debug("Truma: could not register the pairing agent: %s", exc)
    try:
        yield registered
    finally:
        if registered and agent_manager is not None:
            try:
                await agent_manager.call_unregister_agent(_AGENT_PATH)
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Truma agent unregister failed: %s", exc)


async def _bond_over_local_link(
    name: str,
    client: BleakClientWithServiceCache,
    *,
    deadline: float | None = None,
) -> tuple[bool, bool]:
    """Bond over the local link Home Assistant already dialled.

    The same exchange ``_bond_over_link`` runs for a proxy, plus the agent the
    local path needs. What matters is what it does **not** do: no second dial.
    The ACL this runs on is the one ``establish_connection`` opened, so there is
    no link of BlueZ's own to be left behind and nothing to release afterwards.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        async with _agent_registered(bus) as registered:
            if not registered:
                LOGGER.debug(
                    "Truma %s: no pairing agent, so a bond over this link "
                    "cannot be confirmed",
                    name,
                )
            return await _bond_over_link(name, client, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        LOGGER.debug("Truma %s: bonding over the dialled link: %s", name, exc)
        return False, True
    finally:
        bus.disconnect()


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

    The identity address is matched first, then the name, then the panel's
    service UUID -- which is the only one of the three a panel in add-device
    mode offers, since it drops its local name there and every address it puts
    on air is a fresh RPA (#31).
    """
    address = address.upper()
    name_lc = name.lower()
    matches: list[tuple[bool, int, str]] = []
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
        uuids = dev.get("UUIDs")
        if dev_addr_v == address:
            how = 0
        elif name_lc and name_lc in dev_name_v.lower():
            how = 1
        elif uuids and has_truma_uuid(list(uuids.value)):
            how = 2
        else:
            continue
        # BlueZ carries RSSI only while it is actually seeing the device, and
        # drops it when the device goes away. An object without one is a
        # leftover -- which is what the identity address becomes the moment a
        # bond is dropped, while the panel goes on advertising fresh RPAs
        # (measured on the van, 2026-09-18: Pair() spent a whole minute on an
        # identity object nothing was behind). Being seen beats how it matched.
        matches.append(("RSSI" not in dev, how, path))
    return min(matches)[2] if matches else None


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


async def async_host_bond_state(
    name: str, address: str, adapter_path: str | None = None
) -> bool | None:
    """Whether BlueZ holds a bond record for this panel: True / False / None.

    **Only the absence is load-bearing.** ``Paired == False`` on the device
    object reliably means this host holds no bond for the panel — measured on
    the van 2026-10-26: with the record deleted and the panel keeping its own
    half, one Reconfigure burned 11 connects and ~60 s to report
    ``pairing_failed``, and the recovery that worked was *clear the panel's
    device list, then arm it* (status file §4q). ``Paired == True`` proves
    nothing — a signature-only record reports it — so a caller may use this to
    warn about a missing bond and never to claim a working one.

    ``None`` means the question could not be answered: no system bus, or the
    panel is not in BlueZ's object list at all (a bond file can outlive the
    object, e.g. before the panel is first heard after a reboot). Say nothing
    rather than guess.
    """
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:  # noqa: BLE001 - a hint is optional; never fail the flow
        return None
    try:
        object_manager = await _get_interface(
            bus, "/", "org.freedesktop.DBus.ObjectManager"
        )
        objects = await object_manager.call_get_managed_objects()
        path = _find_device(
            objects, name=name, address=address, adapter_path=adapter_path
        )
        if path is None:
            return None
        return _is_paired(objects, path)
    except Exception:  # noqa: BLE001 - reported as "unknown", not as a failure
        return None
    finally:
        bus.disconnect()


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
    trust_existing_bond: bool = True,
    may_forget_bond: bool = False,
) -> bool:
    """Bond the Truma panel over local BlueZ (D-Bus). Return ``True`` if bonded.

    Registers a temporary Just Works agent and busy-loops ``Device1.Pair()``
    until the panel reports ``Paired`` or ``timeout`` elapses. The caller must
    have prompted the user to put the panel into add-device mode (and to clear
    its device list if it is full).

    ``adapter_path`` (e.g. ``/org/bluez/hci0``) scopes the bond to the adapter
    HA connects through; when omitted, any adapter that sees the panel is used.

    ``may_forget_bond`` is the user's consent to drop an existing bond on this
    adapter when the panel refuses it. It defaults to ``False`` because the
    evidence does not distinguish a stale key from an unarmed panel, and the
    caller is the only party that asked -- see the removal branch below.

    ``trust_existing_bond`` is whether BlueZ reporting ``Paired`` may be taken
    as the answer. Normally it may: the caller got as far as a link before
    handing over, so a bond on this adapter is one the panel honours. It may
    not when the caller has never seen the panel answer one -- because nothing
    would establish a link (#26), or because it has not dialled yet -- and a
    bond is half-held by definition then. BlueZ will offer a key the panel has
    forgotten and the panel drops the link the moment it cannot decrypt, which
    is indistinguishable from a panel that was never paired, so a host-side
    ``Paired`` would report success against a panel that has seen nothing.

    ``Paired`` is not even evidence that a key exists. BlueZ reports it (and
    ``Bonded``) for a stored record carrying nothing but the signature keys,
    which is what the van's adapter held on 2026-09-18 after the panel's own
    bond was deleted: no ``LongTermKey``, no session possible, and a fast path
    that believed it answered in under a millisecond.

    Unproven, the order is try **then** remove: ``Device1.Pair()`` goes first
    and the bond is dropped only once that has failed with BlueZ still claiming
    ``Paired``. Removing up front would be the cheaper code and the worse
    behaviour -- re-pairing is the one path a user reaches with a *working*
    bond (Reconfigure), and a panel that is not in add-device mode at that
    moment would be left with no bond at all. One removal *attempt* per call,
    and only after the panel has refused: a removal that does not go through
    leaves the bond suspect, so the call runs out its timeout rather than
    reporting a success it cannot see -- and does not spend that timeout
    retrying a D-Bus call that has already said no.

    BlueZ transport only. Safe to call when already bonded (returns quickly).
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = _JustWorksAgent()
    registered = False
    adapter_iface = None
    # The BlueZ object this attempt worked on, and whether the link over it has
    # already been let go. Kept outside the ``try`` so the ``finally`` can clean
    # up after a failure as well as a success -- see the release there.
    path: str | None = None
    link_released = False
    try:
        object_manager = await _get_interface(
            bus, "/", "org.freedesktop.DBus.ObjectManager"
        )

        # Fast path: already bonded (on the connecting adapter)? Not open to
        # a caller that has not seen the panel answer a link -- see
        # trust_existing_bond.
        objects = await object_manager.call_get_managed_objects()
        path = _find_device(
            objects, name=name, address=address, adapter_path=adapter_path
        )
        # Whether a bond record was already there when we started. The loop's
        # post-timeout re-check may only call a bond "made" if there was none
        # before -- otherwise it would bless the very stale record this call may
        # have been trying to get rid of.
        had_bond_at_start = bool(path and _is_paired(objects, path))
        if trust_existing_bond and _already_bonded(
            objects, path=path, adapter_path=adapter_path
        ):
            LOGGER.debug("Truma %s already bonded on %s", name, adapter_path)
            assert path is not None  # _already_bonded is False without one
            await _release_link(bus, path, name)
            link_released = True
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

        # Ask BlueZ to look for the panel itself. Home Assistant's scanner
        # reads adverts off the MGMT socket, which creates no Device1 objects,
        # so without our own discovery the only objects on the bus are
        # leftovers -- and Pair() can only be called on an object. This is why
        # a manual ``bluetoothctl scan on`` always found the panel while
        # pairing did not (measured on the van, 2026-09-18).
        adapter_iface = None
        if adapter_path:
            try:
                adapter_iface = await _get_interface(
                    bus, adapter_path, "org.bluez.Adapter1"
                )
                await adapter_iface.call_set_discovery_filter(
                    {"Transport": Variant("s", "le"), "DuplicateData": Variant("b", True)}
                )
                await adapter_iface.call_start_discovery()
                LOGGER.debug("Truma %s: BlueZ discovery started for pairing", name)
            except Exception as exc:  # noqa: BLE001 - best effort
                LOGGER.debug("Truma %s: could not start BlueZ discovery: %s", name, exc)

        LOGGER.info("Truma %s: attempting Just Works bond (%ss)", name, timeout)
        start = time.monotonic()
        # Whether a bond BlueZ reports is one this call can believe. A bond
        # that predates the call may be half-held -- the panel discarded its
        # key and BlueZ kept ours -- and stops being suspect once we have
        # bonded it ourselves or dropped the old one. The drop gets one attempt
        # either way, so a RemoveDevice the daemon refuses cannot turn the
        # remaining timeout into a retry loop.
        suspect = not trust_existing_bond
        removal_tried = False
        removal_skipped = False
        pair_pending_until = 0.0
        # The object the last Pair() was issued against. BlueZ pairs one device
        # at a time per adapter, so a call still running against the wrong one
        # is not just a wasted poll: it answers every other object InProgress
        # too, and the loop cannot pair anything until it is called off.
        pending_path: str | None = None
        pending_since = 0.0
        # Objects the kernel has been given connection parameters for. A set
        # rather than the last one: they are stored per address, so a rotation
        # needs its own load -- but the loop can be handed the same two objects
        # alternately for a whole minute, and reloading on every flip would put
        # sixty MGMT commands on the socket to say the same two things.
        params_loaded: set[str] = set()
        while time.monotonic() - start < timeout:
            objects = await object_manager.call_get_managed_objects()
            # BlueZ first: it is what Pair() is called on, and it knows
            # which of its objects it can currently see. Home Assistant's
            # resolver is the fallback for the case where BlueZ has nothing
            # yet but the panel has been heard.
            path = _find_device(
                objects, name=name, address=address, adapter_path=adapter_path
            ) or _live_device_path(hass, name, adapter_path)
            # Dropping this host's bond takes the panel's IRK with it, so the
            # identity address stops resolving to the RPA the panel is actually
            # advertising on and a Pair() against it can never connect. The
            # object to pair moves at exactly that moment, and the call already
            # running has to be called off for the new one to be taken at all.
            if pending_path is not None and path is not None and path != pending_path:
                LOGGER.debug(
                    "Truma %s: the panel is on %s now; cancelling the pairing "
                    "still running against %s",
                    name,
                    path,
                    pending_path,
                )
                await _cancel_pairing(bus, pending_path, name)
                pair_pending_until = 0.0
                pending_path = None
            elif pending_path is not None and pending_since and (
                time.monotonic() - pending_since > _PAIR_PENDING_LIMIT
            ):
                LOGGER.debug(
                    "Truma %s: the pairing on %s has run %.0fs without "
                    "finishing; cancelling it and asking again",
                    name,
                    pending_path,
                    time.monotonic() - pending_since,
                )
                await _cancel_pairing(bus, pending_path, name)
                pair_pending_until = 0.0
                pending_path = None
            if path and _is_paired(objects, path) and not suspect:
                LOGGER.info("Truma %s bonded", name)
                await _release_link(bus, path, name)
                link_released = True
                return True
            if path and time.monotonic() < pair_pending_until:
                # BlueZ is still working on the call we already made. Watch the
                # Paired property instead of asking again -- a second Pair()
                # only earns an InProgress and throws away the poll.
                LOGGER.debug(
                    "Truma %s: a pairing attempt is still running on %s",
                    name,
                    pending_path or path,
                )
            elif path:
                if path not in params_loaded:
                    _load_fast_conn_params(objects, path, name, adapter_path)
                    params_loaded.add(path)
                LOGGER.debug("Truma %s: pairing %s", name, path)
                failure = await _try_pair(bus, path)
                if failure is not None:
                    # Whatever BlueZ answered, it may be carrying the call --
                    # a client-side timeout at _PAIR_CALL_TIMEOUT is the one
                    # that says nothing, and the daemon goes on regardless.
                    if pending_path is None:
                        pending_since = time.monotonic()
                    pending_path = path
                if failure is None:
                    # Ours now, whatever was there before.
                    suspect = False
                    pending_path = None
                    pending_since = 0.0
                elif _is_in_progress(failure):
                    pair_pending_until = time.monotonic() + _PAIR_PENDING_WAIT
                elif suspect and may_forget_bond and not removal_tried and _is_paired(
                    objects, path
                ):
                    # Try-then-remove, and only with the user's consent.
                    #
                    # The panel refused while BlueZ still claims a bond, which
                    # has two readings this loop cannot tell apart: this host
                    # holds a key the panel has forgotten (remove it, the next
                    # pass pairs clean) or the panel is simply not in add-device
                    # mode, where it refuses *every* link it is offered. Not
                    # conditioned on the error text on purpose -- BlueZ words
                    # both readings the same way (AlreadyExists,
                    # AuthenticationFailed), so a "stale key" heuristic built on
                    # the text would be a guess. The only evidence that
                    # separates them belongs to the user, who knows whether the
                    # panel is armed -- so ``may_forget_bond`` is their answer,
                    # never this module's inference. Removing a working bond
                    # after a mistimed Reconfigure is the one outcome here that
                    # cannot be undone from Home Assistant.
                    removal_tried = True
                    if await _forget(bus, path, name, adapter_path=adapter_path):
                        suspect = False
                elif suspect and _is_paired(objects, path) and not removal_skipped:
                    # Same evidence as the branch above, without the consent:
                    # say so once, in words a support log can carry, and leave
                    # the bond alone.
                    removal_skipped = True
                    LOGGER.warning(
                        "Truma %s: the panel refused, and BlueZ still holds a "
                        "bond on %s. Leaving it in place: a panel that is not "
                        "in add-device mode refuses exactly like one that has "
                        "forgotten this host, so dropping the bond on this "
                        "evidence can destroy a working one. Put the panel into "
                        "add-device mode and retry; if the bond really is "
                        "stale, retry with 'forget the stale bond' selected.",
                        name,
                        path,
                    )

            else:
                LOGGER.debug("Truma %s: no device object to pair yet", name)
            await asyncio.sleep(_POLL_INTERVAL)

        LOGGER.warning("Truma %s: pairing timed out after %ss", name, timeout)
        # The deadline is not the truth. Measured on the van 2026-10-24: two
        # Reconfigure attempts both returned pairing_failed at 60.0 s -- the
        # bond landed afterwards, with a complete LTK+IRK record and a live
        # session. Reporting that as a failure is the mirror of 0.7's false
        # success, and it is actively harmful: the error text tells the user to
        # try again, and a retry on a build without the consent gate deletes
        # the bond that was just written. So look once more before saying no.
        for wait in (0.0, 3.0):
            if wait:
                await asyncio.sleep(wait)
            try:
                objects = await object_manager.call_get_managed_objects()
                path = _find_device(
                    objects, name=name, address=address, adapter_path=adapter_path
                )
                if path and _is_paired(objects, path) and not had_bond_at_start:
                    LOGGER.info(
                        "Truma %s bonded -- the bond landed after the %ss "
                        "deadline, so this is a success, not a timeout",
                        name,
                        timeout,
                    )
                    await _release_link(bus, path, name)
                    link_released = True
                    return True
            except Exception as exc:  # noqa: BLE001 - never fail on the re-check
                LOGGER.debug("Truma %s: post-timeout bond check: %s", name, exc)
        return False
    finally:
        # A pairing that **failed** can leave the ACL Pair() opened half up, and
        # nothing else owns it: BlueZ keeps it, Device1 still reports
        # ``Connected: no``, and the kernel then refuses every dial to a peer it
        # believes it is connected to. Measured on the van 2026-10-25 after a
        # Reconfigure against an idle panel (an ACL in BT_CONFIG at handle 3840,
        # every later connect timing out). The paths that report a bond already
        # release it themselves; this is the one that does not, and it must run
        # before discovery stops, because stopping it makes BlueZ drop the
        # device object this needs.
        if not link_released and path is not None:
            await _release_link(bus, path, name)
        if adapter_iface is not None:
            try:
                await adapter_iface.call_stop_discovery()
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Truma stop discovery failed: %s", exc)
        if registered:
            try:
                await agent_manager.call_unregister_agent(_AGENT_PATH)
            except Exception as exc:  # noqa: BLE001 - best effort cleanup
                LOGGER.debug("Truma agent unregister failed: %s", exc)
        bus.disconnect()


def _device_address(objects: dict, path: str) -> tuple[str, bool] | None:
    """``(address, is_random)`` for a BlueZ device path, or ``None``.

    Prefers what BlueZ says over what the path spells, and falls back to the
    path for an object the resolver found that this pass of GetManagedObjects
    did not carry. A panel in add-device mode is only ever on a resolvable
    private address, which is random, so that is the fallback.
    """
    dev = objects.get(path, {}).get("org.bluez.Device1")
    if dev:
        addr = dev.get("Address")
        kind = dev.get("AddressType")
        if addr is not None:
            return (
                str(addr.value).upper(),
                kind is None or str(kind.value).lower() != "public",
            )
    tail = path.rsplit("/", 1)[-1]
    if tail.startswith("dev_"):
        return tail[4:].replace("_", ":").upper(), True
    return None


def _load_fast_conn_params(
    objects: dict, path: str, name: str, adapter_path: str | None
) -> None:
    """Give the bond the connection parameters Home Assistant's own dials get.

    ``Device1.Pair()`` makes BlueZ dial the panel itself, and BlueZ dials with
    the kernel's stock parameters unless something has loaded better ones for
    that address. Home Assistant always has: every connection habluetooth makes
    is preceded by an MGMT ``Load Connection Parameters`` carrying its FAST
    preset. Nothing does it for us, because nothing else in Home Assistant
    reaches past habluetooth to BlueZ the way this function's caller has to.

    The difference is not marginal. Measured on the van (2026-09-18), one
    adapter, same minutes, from the same btmon capture::

        peer                 interval        supervision timeout   outcome
        14:9C:EF:03:68:81    7.50 ms         10000 ms              65x success
        C4:D3:6A:8C:B5:38    7.50 ms         10000 ms              90x success
        the panel            30.00-50.00 ms    420 ms              connects, dies

    420 ms at a 45 ms connection interval is nine connection events of budget.
    The link came up -- ``LE Connection Complete, Status: Success`` -- and was
    gone 274 ms later, six events, which is the link-layer limit for
    establishment: the panel had not got a single packet in before the central
    gave up. Every one of the 73 attempts that day failed that way, none
    reached SMP, and from D-Bus it looked like ``le-connection-abort-by-local``
    -- our side, not a panel refusing to pair.

    Best effort throughout. Pairing with the stock parameters is what this did
    before and it did sometimes work, so a habluetooth that has moved this API
    or a host with no MGMT socket costs us what we already had.
    """
    if not adapter_path:
        return
    try:
        from habluetooth import get_manager
        from habluetooth.const import (
            BDADDR_LE_PUBLIC,
            BDADDR_LE_RANDOM,
            ConnectParams,
        )
    except ImportError:  # pragma: no cover - habluetooth always present in HA
        return
    found = _device_address(objects, path)
    if found is None:
        return
    address, is_random = found
    try:
        index = int(adapter_path.rsplit("hci", 1)[-1])
    except ValueError:
        return
    try:
        mgmt = get_manager().get_bluez_mgmt_ctl()
        if mgmt is None:
            LOGGER.debug("Truma %s: no MGMT socket; pairing %s as-is", name, address)
            return
        loaded = mgmt.load_conn_params(
            index,
            address,
            BDADDR_LE_RANDOM if is_random else BDADDR_LE_PUBLIC,
            ConnectParams.FAST,
        )
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.debug("Truma %s: loading connection parameters failed: %s", name, exc)
        return
    LOGGER.debug(
        "Truma %s: %s fast connection parameters for %s",
        name,
        "loaded" if loaded else "could not load",
        address,
    )


def _is_in_progress(failure: str) -> bool:
    """Whether a failed ``Pair()`` means "already pairing" rather than "no".

    BlueZ answers ``org.bluez.Error.InProgress`` while a pairing it accepted
    earlier is still running -- including one this module started and stopped
    waiting for at ``_PAIR_CALL_TIMEOUT``. Treating that as a refusal turns the
    loop into a caller of a call it has already made.
    """
    return "inprogress" in failure.replace(" ", "").replace(".", "").lower()


async def _cancel_pairing(bus: MessageBus, path: str, name: str) -> None:
    """Call off a pairing BlueZ is still running against ``path``.

    Best effort, and usually against an object that is on its way out: the
    reason to cancel is that the panel has moved to an address this one cannot
    reach, and the drop that moved it may already have taken the object with
    it. What matters is the daemon's side -- while it carries a pairing, every
    Pair() on any other object earns InProgress, so the panel's live address
    cannot be paired until this one is released.
    """
    try:
        device = await _get_interface(bus, path, "org.bluez.Device1")
        await asyncio.wait_for(
            device.call_cancel_pairing(), timeout=_PAIR_CALL_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.debug("Truma %s: cancelling the pairing on %s: %s", name, path, exc)


async def _try_pair(bus: MessageBus, path: str) -> str | None:
    """One pairing attempt against the device at ``path`` (best effort).

    ``None`` means the bond took. Anything else is the failure text, which the
    caller needs in order to tell "the panel has not accepted yet" from "this
    host holds a key the panel has forgotten" -- it cannot read that off the
    string, but it can read it off a failure landing beside a ``Paired`` that
    was already there.
    """
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
        return str(exc)[:80] or type(exc).__name__
    return None


async def _release_link(bus: MessageBus, path: str, name: str) -> None:
    """Drop the link the bond was made over, so the session can dial the panel.

    ``Device1.Pair()`` brings up an ACL of its own and BlueZ keeps it after the
    bond completes: encrypted, services resolved, owned by nobody. Home
    Assistant then cannot connect at all. The kernel refuses a second link to a
    peer it is already connected to, so every dial fails instantly --

        Failed to connect after 3 attempt(s):
        [org.bluez.Error.Failed] Input/output error

    -- and the panel, being connected, stops advertising, so the resolver sees
    it go stale on top. Measured on the van (2026-09-18): a bond at 11:24:42
    left that link up, 32 connects failed against it over the next fifteen
    minutes, and dropping it by hand changed the failure the same second. The
    integration looked exactly like a panel that had paired and then vanished
    -- one device, one entity, disconnected.

    Not a hand-over: the proxy path returns its live client for the coordinator
    to adopt, and doing the same here would mean handing out a ``BLEDevice``
    pointing at this object path. That is worth doing, and it is not what this
    is. One extra dial costs a couple of seconds and needs no new machinery.

    Best effort throughout, and silent when there is no link: a bond that
    succeeded is not undone by a disconnect that does not.
    """
    try:
        properties = await _get_interface(
            bus, path, "org.freedesktop.DBus.Properties"
        )
        connected = await properties.call_get("org.bluez.Device1", "Connected")
        if not connected.value:
            return
        device = await _get_interface(bus, path, "org.bluez.Device1")
        await asyncio.wait_for(
            device.call_disconnect(), timeout=_DISCONNECT_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - best effort cleanup
        LOGGER.debug("Truma %s: releasing the pairing link: %s", name, str(exc)[:80])
    else:
        LOGGER.debug(
            "Truma %s: released the link the bond was made over (%s)", name, path
        )


async def _forget(
    bus: MessageBus, path: str, name: str, *, adapter_path: str | None = None
) -> bool:
    """Drop the host's own bond for the panel, so the next ``Pair()`` is fresh.

    ``Adapter1.RemoveDevice`` is the only way to clear a bond BlueZ holds; the
    panel has no say in it and does not need one, because the key being removed
    is the half the panel has already discarded. The adapter comes from the
    device path when the caller has not scoped one, since a device object always
    hangs off the adapter that knows it.

    Returns whether the bond is gone. A failure here is not fatal -- the caller
    keeps retrying ``Pair()`` and keeps treating the old bond as suspect, which
    is the state it was in anyway.
    """
    adapter = adapter_path or path.rsplit("/", 1)[0]
    try:
        interface = await _get_interface(bus, adapter, "org.bluez.Adapter1")
        await asyncio.wait_for(
            interface.call_remove_device(path), timeout=_PAIR_CALL_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.debug("Truma %s forget bond: %s", name, str(exc)[:80])
        return False
    LOGGER.info(
        "Truma %s: dropped this host's bond (%s); the panel had refused it",
        name,
        path,
    )
    return True
