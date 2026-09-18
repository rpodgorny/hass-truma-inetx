"""Shared Bluetooth resolution for the Truma iNet X panel.

The panel uses a rotating Resolvable Private Address, so a stored MAC goes
stale. Both the live session (coordinator) and the onboarding bond (config
flow) have to find the panel the same way, and this module is the single
source of that resolution.

What it resolves is an **address**, not a route. Which adapter carries the
connection is Home Assistant's decision: habluetooth swaps
``bleak_retry_connector.BleakClientWithServiceCache`` for its own wrapper, and
that wrapper throws away the scanner the BLEDevice came from, keeps only the
address, and re-picks the best connectable path at connect time -- scoring
every candidate by RSSI, by how many connects to *that address* have already
failed on that scanner, by connects in flight, and by free connection slots.
Nothing in HA pins a device to a scanner, so a transport preference expressed
here is overruled a moment later anyway. This module used to hold one; it only
made the wrong address get tried first (issue #13).

Nothing here asks which transport it got, either. The one caller that needs
to know -- ``pairing``, where bonding *is* the transport -- reads it off the
connected client instead (``ble.client_is_proxy``). ``local_only`` below is
the single remaining exception, and it is a data requirement rather than a
preference.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER, has_truma_uuid, looks_like_panel

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

# How recently the panel must have been heard for a connect to be worth
# starting, and how long to wait for that to happen.
ADVERT_FRESH_SECONDS = 5.0
ADVERT_WAIT_TIMEOUT = 30.0


def is_panel_advert(info: BluetoothServiceInfoBleak) -> bool:
    """Return True when this advertisement belongs to a Truma panel.

    The rule itself is :func:`~.const.looks_like_panel`, shared with the
    Home-Assistant-free ``bus`` scan. Whether an advert that passes it can also
    *key* a config entry is a separate question -- see :func:`advert_name`.
    """
    return looks_like_panel(info.name, info.service_uuids)


def _as_address(value: str) -> str:
    """``value`` as bare address hex, or ``""`` when it is not an address.

    Separators only -- a name is not reduced to the hex it happens to contain,
    so "Truma iNetX-FFB4D1" stays a name. Twelve hex digits or nothing.
    """
    bare = value.upper()
    for sep in (":", "-", "_", " "):
        bare = bare.replace(sep, "")
    if len(bare) == 12 and all(c in "0123456789ABCDEF" for c in bare):
        return bare
    return ""


def advert_name(info: BluetoothServiceInfoBleak) -> str | None:
    """The panel's stable advertised name, or ``None`` if it has not given one.

    Home Assistant substitutes the address when an advertisement carries no
    local name, which happens whenever the scan response does not arrive --
    under passive scanning always, and on the panel this was written against in
    add-device mode as well. (Not universal: a Panel 2 keeps its name in
    add-device mode, measured in issue #6.) That address is a rotating RPA, so
    keying anything on it produces a fresh, MAC-titled discovery every rotation
    instead of one correctly-named panel. Callers that need a key must wait for
    a named advert -- and, on a scanner that is not actively scanning, ask for
    one first: see :func:`async_sweep_for_names`.

    The address does not always come back spelled the way ``info.address``
    spells it. Home Assistant substitutes the colon form, but BlueZ's own
    fallback for a device it has no name for is the address with dashes, and
    that reaches us as a name whenever the object it came from is one BlueZ
    made itself. Measured on the van (2026-09-18): a bond completed while the
    panel was in add-device mode and still nameless, and the config entry it
    produced was keyed ``4D-6B-5F-62-51-68`` -- unique_id, title and stored
    name all a private address due to rotate within the quarter hour. So this
    compares addresses as addresses, not as strings.
    """
    if not info.name:
        return None
    if (bare := _as_address(info.name)) and bare == _as_address(info.address):
        return None
    return info.name


# How long an on-demand active window runs. Home Assistant clamps it to
# habluetooth's 5..35s; ten seconds is several advertising intervals of a panel
# that sends roughly one a second.
SWEEP_SECONDS = 10.0
# How long to leave the bus alone afterwards. Every advertisement the panel
# sends reaches the discovery step, a few a second, and each one of them finds
# no name -- so without this the first nameless panel in range would ask for a
# window continuously.
SWEEP_COOLDOWN = 120.0
# How long to keep asking. One request is not one window: the scheduler skips
# any scanner that is mid-connect, and on a host with a single adapter and
# other integrations polling devices over it, that is a large slice of the
# time. Measured on the van (2026-09-18): the first window asked for opened
# nothing at all, and habluetooth said why -- "connect in progress and no
# fallback scanner". A second adapter would have taken the window; there isn't
# one, so the answer is to come back in a moment and ask again.
SWEEP_DEADLINE = 30.0
# Pause between requests. A refused one returns at once, so without this the
# retry would spin rather than wait for the connect to finish.
SWEEP_RETRY_PAUSE = 2.0
_SWEEP_LAST = f"{DOMAIN}_name_sweep"


def any_panel_named(hass: HomeAssistant) -> bool:
    """Whether some advertisement that looks like a panel now carries a name.

    The stop condition for a sweep started by the discovery step, which has
    already returned and has no flow state left to look at.
    """
    return any(
        advert_name(info) is not None
        for info in bluetooth.async_discovered_service_info(hass, connectable=False)
        if is_panel_advert(info)
    )


async def async_sweep_for_names(
    hass: HomeAssistant,
    *,
    cooldown: float = 0.0,
    until: Callable[[], bool] | None = None,
) -> bool:
    """Ask the AUTO-mode scanners for active scans. Was anything asked for?

    A name reaches Home Assistant only in a scan response, and only an active
    scan asks for one. Home Assistant's default scanning mode is AUTO, which
    starts passive and turns the radio active only for scheduled windows: once
    four minutes after the scanner starts, then once every twelve hours
    (habluetooth ``AUTO_INITIAL_SWEEP_DELAY`` / ``AUTO_REDISCOVERY_INTERVAL``,
    fifteen seconds a window). There are per-device windows too, but only for
    an address some integration registered a Bluetooth callback on, and this
    one registers none -- it reads the advertisement history instead.

    Outside those windows nothing solicits a scan response, so nothing sends
    one. Measured on the van (2026-09-18): forty-five seconds of passive
    scanning, 468 advertising reports, not one Name field from any device on
    the bus. The panel's advertisement proper is twenty-one bytes -- flags, and
    one Truma service UUID -- which is enough to *recognise* a panel and not
    enough to *key* one. So discovery aborted "awaiting_name" on every advert
    and the manual step reported "no devices found", on a panel sitting there
    advertising, with no way to tell that from not hearing it at all.

    It only looks like it works on a host that has bonded the panel before:
    BlueZ keeps the name in ``/var/lib/bluetooth/<adapter>/<addr>/info`` and
    hands it over whatever the scan mode. Drop the bond and the name goes with
    it, which is exactly the state a first pairing is in.

    ``cooldown``: skip if a sweep was asked for that recently. A sweep is
    bus-wide and concurrent callers dedupe into one window, so the cost of
    asking is small -- but not small enough to ask several times a second.

    ``until``: checked after each window; stop as soon as it is true. Without
    one, keep asking until ``SWEEP_DEADLINE``, since a request the scheduler
    refused returns success-shaped and there is nothing else to tell us.

    ACTIVE and PASSIVE scanners ignore the request, so this changes nothing on
    a host that is already active-scanning, and cannot rescue one pinned to
    passive: no window is ever opened there and no name is ever learnt.
    """
    try:
        from homeassistant.components.bluetooth import async_request_active_scan
    except ImportError:  # pragma: no cover - Home Assistant before 2026.9
        return False
    now = time.monotonic()
    last = hass.data.get(_SWEEP_LAST)
    if cooldown and last is not None and now - last < cooldown:
        return False
    hass.data[_SWEEP_LAST] = now
    deadline = now + SWEEP_DEADLINE
    while True:
        LOGGER.debug(
            "Truma: asking for a %ss active scan to learn panel names", SWEEP_SECONDS
        )
        try:
            await async_request_active_scan(hass, SWEEP_SECONDS)
        except Exception as exc:  # noqa: BLE001 - discovery must survive it
            LOGGER.debug("Truma: active scan request failed: %s", exc)
            return False
        if until is not None and until():
            return True
        if time.monotonic() >= deadline:
            LOGGER.debug(
                "Truma: %ss of active scans learnt no panel name", SWEEP_DEADLINE
            )
            return True
        await asyncio.sleep(SWEEP_RETRY_PAUSE)


async def async_known_name(address: str) -> str | None:
    """The panel's name as BlueZ remembers it for ``address``, or ``None``.

    The last resort behind :func:`advert_name`, and the only thing that works
    at all once the panel is bonded. Measured on the van (2026-09-18): after a
    successful bond the panel stopped answering scan requests entirely -- three
    active windows, not one scan response -- so nothing on air carried a name
    any more, and re-adding the integration had nothing to key on. BlueZ had it
    the whole time, stored beside the keys::

        /var/lib/bluetooth/E4:5F:01:0B:37:DD/50:98:93:FF:B4:D1/info
        [General]
        Name=Truma iNetX-FFB4D1

    Home Assistant does not pass it on: advertisements reach it over an MGMT
    side channel carrying raw AD bytes, and ``local_name or device.name or
    address`` never consults the BlueZ object. So ask BlueZ directly.

    ``Name`` in preference to ``Alias``: the first is what the panel called
    itself, the second is what somebody may have renamed it to locally, and a
    unique_id wants the one the panel will still answer to.

    Imported where it is used: ``ble`` is the transport, and this module is
    deliberately not built on top of it.
    """
    from .ble import device_from_bluez

    device = await device_from_bluez(address)
    if device is None:
        return None
    details = getattr(device, "details", None)
    props = details.get("props") if isinstance(details, dict) else None
    name = (props or {}).get("Name") or getattr(device, "name", None)
    if not name:
        return None
    name = str(name)
    if (bare := _as_address(name)) and bare == _as_address(address):
        # BlueZ's own fallback for a device it has no name for either.
        return None
    return name


def async_sweep_for_names_soon(hass: HomeAssistant) -> None:
    """Start :func:`async_sweep_for_names` in the background, rate-limited.

    For callers that must answer now and cannot wait out the window -- the
    discovery step, which has to return an abort while the sweep it just asked
    for is still running. The named advert then arrives on its own and opens a
    fresh flow, which is how that step already expects to be reached.
    """
    hass.async_create_background_task(
        async_sweep_for_names(
            hass, cooldown=SWEEP_COOLDOWN, until=partial(any_panel_named, hass)
        ),
        "truma_inetx active scan for panel names",
        eager_start=True,
    )


def is_remote_scanner(scanner: object) -> bool:
    """Return True for a remote (e.g. ESP32 proxy) scanner, not a local adapter.

    Only ``local_only`` below has any business calling this. Nothing else may
    branch on the kind of scanner an advert came from: the scanner that heard
    the panel is not the adapter that will carry the connection.
    """
    try:
        from habluetooth import BaseHaRemoteScanner
    except ImportError:  # pragma: no cover - habluetooth always present in HA
        return False
    return isinstance(scanner, BaseHaRemoteScanner)


def _panel_infos(hass: HomeAssistant, name: str) -> list:
    """Every advert that looks like this panel, seen by any scanner.

    The name test is exact rather than the prefix :func:`is_panel_advert`
    accepts -- this asks about *this* panel, not any panel. The service UUID is
    an equal-standing match rather than a fallback: the panel this integration
    was written against drops its local name in add-device mode, and under
    passive scanning no panel gives one at all.
    """
    return [
        info
        for info in bluetooth.async_discovered_service_info(hass, connectable=False)
        if info.name == name or has_truma_uuid(info.service_uuids)
    ]


def async_panel_advertising(hass: HomeAssistant, name: str) -> bool:
    """Return True when the panel is being heard at all, by any scanner.

    Separates the two reasons :func:`async_resolve_device` returns ``None``:
    we hear the panel but nothing connectable can reach it, versus we hear
    nothing at all (panel off, out of range, or asleep). They are
    indistinguishable to the resolver and need opposite advice, so only the
    first should ever tell a user to go buy hardware.
    """
    return bool(_panel_infos(hass, name))


# The panel puts two kinds of address on air. The identity address never
# rotates -- its last three bytes are the suffix in the panel's name, e.g.
# ``...FFB4D1`` for "Truma iNetX-FFB4D1" -- while everything else is a
# Resolvable Private Address that changes every few minutes. Which of the two
# a given host can actually connect on is a property of that host's kernel and
# controller -- ``docs/connectivity.md`` has which kernel does what, and why
# 6.19 changed it -- so it is worth remembering rather than guessing: see
# ``prefer_identity``.
ADDR_IDENTITY = "identity"
ADDR_RPA = "rpa"

# How far behind the panel's freshest advert a candidate may be and still be
# treated as on air. The panel rotates its RPA every few minutes, so anything
# this far back has most likely been left behind; Home Assistant keeps such an
# entry for much longer, with the rssi it last had or -127 for none at all.
# Deliberately generous: a live address can itself go a while unheard where a
# static advert is reported once per discovery session (#31), and the test is
# relative -- when every candidate is equally old, none of them is stale.
STALE_ADVERT_SECONDS = 120.0


def address_kind(name: str, address: str) -> str:
    """Return whether ``address`` is the panel's identity address or an RPA.

    Falls back to ``ADDR_RPA`` when the name carries no usable suffix: the
    identity address is only recognisable *through* that suffix, so without one
    there is nothing to claim.
    """
    suffix = name.rsplit("-", 1)[-1].upper()
    if len(suffix) != 6 or any(c not in "0123456789ABCDEF" for c in suffix):
        return ADDR_RPA
    if address.replace(":", "").upper().endswith(suffix):
        return ADDR_IDENTITY
    return ADDR_RPA


def async_has_proxy_route(hass: HomeAssistant, name: str) -> bool:
    """Whether a remote (proxy) scanner can currently reach the panel.

    This is not a transport choice and must not become one -- Home Assistant
    scores every connectable path at connect time and this module has no say
    in it. It answers one narrower question, for pairing only: may this host
    bond over its own adapter *before* trying to connect?

    Where no proxy can hear the panel, yes. The bond then lands on the only
    route there is, which is the route the session will use. Where a proxy can
    hear it, no: a local bond on such a host can end up on a path nothing
    connects over, and every later session then fails at encryption -- the
    failure c91f711 was written to stop.

    Pairing needs the answer because an unbonded panel drops every link it is
    offered, so on a proxyless host the connect that bonding used to wait for
    can never succeed (#26); see ``ensure_bonded``.
    """
    for info in _panel_infos(hass, name):
        for sd in bluetooth.async_scanner_devices_by_address(
            hass, info.address, connectable=True
        ):
            if is_remote_scanner(sd.scanner):
                return True
    return False


def async_resolve_device(
    hass: HomeAssistant,
    name: str,
    *,
    avoid: Iterable[str] = (),
    local_only: bool = False,
    prefer_identity: bool = False,
) -> BLEDevice | None:
    """Find the address the panel can be dialled on right now.

    Matches the panel by its stable advertised ``name`` OR primary service UUID
    (the local name is absent from add-device/pairing adverts) and returns a
    connectable device for the best-ranked address, or ``None`` when nothing
    connectable can reach it (the caller should retry).

    Which **adapter** ends up carrying that connection is not decided here and
    cannot be -- Home Assistant re-picks the path at connect time; see the
    module docstring. So the device handed back is really an address with a
    scanner attached, and the ranking below is entirely about addresses.

    Fresh beats stale, and the identity address is ranked below the RPAs by
    default: through a proxy it usually dials a stale cached bonded RPA rather
    than the live one. It is still worth a try once the RPAs are exhausted,
    because it is the panel's real on-air address in add-device mode -- and on
    a host whose kernel puts the peer's identity on air, it is the *only*
    address that ever connects. ``prefer_identity`` flips that one comparison
    for a host that has proved this, which is what issue #13 costs otherwise:
    every session walking the RPAs, timing out on each, before reaching the
    address that works. It reorders, it never excludes -- a wrong memory costs
    one attempt, not the connection.

    ``avoid`` is a set of addresses that failed to establish. This exists
    because of a specific, observed failure mode after pairing:

    Right after the bond, the panel keeps *advertising* the RPA it paired on
    but stops *accepting connections* on it (a panel-side phantom from the
    pairing hand-off), while it simultaneously advertises a fresh, live RPA.
    Both addresses look equally valid here — same name, near-identical
    timestamps — but connecting the dead one fails forever with
    ESP_GATT_CONN_FAIL_ESTABLISH (0x3e). Without ``avoid`` we always return the
    same first candidate and the coordinator hammers the dead address
    indefinitely, leaving the device "unavailable" until someone power-cycles
    the panel. The coordinator feeds back each address that failed to establish
    so we rotate to the panel's other advertised RPA instead.

    ``avoid`` **demotes**, it does not exclude. A failed address ranks below
    every address that has not failed, which is enough to rotate off a phantom
    while a live RPA is on air — but when the failed address is the only route
    left we hand it back and let the caller retry, because the set cannot tell
    a phantom from a transient failure. Excluding was a bug (#14): it erased
    the identity address, which never rotates and so can never be the phantom,
    so one failure against it — the panel still holding the slot of a
    just-closed session, say — banished the only route a host connecting over
    the identity has.

    ``local_only`` restricts the answer to what a local adapter can see. It is
    not a preference and not a speed choice, and it has exactly one legitimate
    caller: ``pairing._bluez_path()`` needs the BlueZ object path to call
    ``Device1.Pair()`` on, and only a local adapter's device carries one. There
    is deliberately no mirror of it -- wanting a proxy is what this module used
    to do, and HA overrules it anyway.
    """
    infos = _panel_infos(hass, name)
    avoid_norm = {a.upper() for a in avoid}
    wanted = ADDR_IDENTITY if prefer_identity else ADDR_RPA
    # An address the panel has left goes on advertising in Home Assistant's
    # cache long after it stops answering, and the kind preference used to
    # outrank freshness outright -- so a dead RPA beat a live identity every
    # reconnect (#32), and a dead identity beat the live RPA the panel was
    # pairing on (measured on the van, 2026-09-18: Pair() paged an entry at
    # rssi -127 for a whole minute while the panel advertised at -51). The
    # kind preference is for choosing between addresses that are both on air;
    # it has nothing to say about one that is not.
    newest = max((i.time for i in infos), default=0.0)
    # Rank, never remove -- a stale candidate is still handed back when it is
    # all there is, on the same reasoning as ``avoid``: the cache may simply
    # have missed the advert that would refresh it.
    #
    #   fresh wanted | fresh other kind | stale … | avoided …
    candidates = sorted(
        infos,
        key=lambda i: (
            i.address.upper() in avoid_norm,
            newest - i.time > STALE_ADVERT_SECONDS,
            address_kind(name, i.address) != wanted,
            -i.time,
        ),
    )
    LOGGER.debug(
        "Truma %s candidates (best→worst): %s | demoted after a failure: %s "
        "| prefers the %s address",
        name,
        [
            (i.address, address_kind(name, i.address), round(i.time, 1), i.rssi)
            for i in candidates
        ],
        sorted(avoid_norm),
        wanted,
    )
    for info in candidates:
        for sd in bluetooth.async_scanner_devices_by_address(
            hass, info.address, connectable=True
        ):
            if local_only and is_remote_scanner(sd.scanner):
                continue
            LOGGER.debug(
                # The scanner named here is only the one that heard this
                # advert; HA scores the paths again when the connect starts,
                # and may well use another.
                "Truma %s -> %s (%s), heard by %s (rssi=%s)",
                name,
                info.address,
                address_kind(name, info.address),
                getattr(sd.scanner, "name", type(sd.scanner).__name__),
                getattr(sd.advertisement, "rssi", None),
            )
            return sd.ble_device
    LOGGER.debug("Truma %s: no connectable route to the panel right now", name)
    return None


def _last_heard(hass: HomeAssistant, name: str) -> float | None:
    """Monotonic timestamp of the panel's most recent advert, if any."""
    infos = _panel_infos(hass, name)
    return max((info.time for info in infos), default=None)


async def async_wait_until_heard(
    hass: HomeAssistant,
    name: str,
    *,
    fresh: float = ADVERT_FRESH_SECONDS,
    timeout: float = ADVERT_WAIT_TIMEOUT,
) -> bool:
    """Wait until the panel was heard within ``fresh`` seconds.

    On a host whose controller cannot resolve private addresses, the kernel
    has to put the address it last saw the peer use on air, and it only learns
    that while scanning. A connect attempt stops the scan, so a dial started
    long after the last advert goes out to an address the panel has already
    rotated away from, times out after ~20 s, and blocks scanning for that
    whole time -- which keeps the cached address stale and makes the next
    attempt fail the same way. Dialing only just after an advert breaks that
    loop; it costs nothing where the controller resolves addresses itself.
    """
    deadline = time.monotonic() + timeout
    while True:
        heard = _last_heard(hass, name)
        if heard is not None and time.monotonic() - heard <= fresh:
            return True
        if time.monotonic() >= deadline:
            LOGGER.debug(
                "Truma %s: no advert within %ss (last heard %ss ago); "
                "not dialing a stale address",
                name,
                fresh,
                None if heard is None else round(time.monotonic() - heard, 1),
            )
            return False
        await asyncio.sleep(0.25)
