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
from collections.abc import Iterable
from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import LOCAL_NAME_PREFIX, LOGGER
from .truma.const import SERVICE_UUID

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

# What the panel actually puts in its advertisement. SERVICE_UUID above is the
# GATT service, only visible after connecting, so it never matches an advert.
# Without this the panel can only be recognised by its local name, which lives
# in the scan response and so requires ACTIVE scanning -- under passive scanning
# the panel is invisible even though the radio hears it perfectly.
ADVERT_SERVICE_UUID = "fc310000-f3b2-11e8-8eb2-f2801f1b9fd1"

# What the iNet X Panel 2 advertises instead (issue #6). An nRF Connect capture
# of a Panel 2 -- hardware 1.4, firmware 3.5.38.1 -- shows one advertised
# service, fc310006, plus manufacturer data under Truma's company ID 0x0c73.
# The GATT table behind it is the panel protocol unchanged: fc314001 write +
# notify, fc314002 write-without-response, fc314003 and fc314004 notify, the
# same four characteristics this integration already drives. So only the
# advertised number moved, and matching it is the whole of Panel 2 discovery.
ADVERT_SERVICE_UUID_PANEL2 = "fc310006-f3b2-11e8-8eb2-f2801f1b9fd1"

# Everything the panel puts on air that identifies it as a Truma panel. Every
# UUID here is proprietary to Truma, so any one matching is evidence on its
# own -- no local name required.
PANEL_SERVICE_UUIDS = frozenset(
    {ADVERT_SERVICE_UUID, ADVERT_SERVICE_UUID_PANEL2, SERVICE_UUID}
)

# How recently the panel must have been heard for a connect to be worth
# starting, and how long to wait for that to happen.
ADVERT_FRESH_SECONDS = 5.0
ADVERT_WAIT_TIMEOUT = 30.0


def is_panel_advert(info: BluetoothServiceInfoBleak) -> bool:
    """Return True when this advertisement belongs to a Truma panel.

    The local name is the obvious test, but it is Truma's to change. The iNet X
    Panel 2 (issue #6) is sold as a drop-in replacement for the panel this
    integration was written against; the manifest's service-UUID matchers still
    route it to us, while a renamed advert no longer starts with the prefix the
    original panel uses. Testing the name alone made such a panel invisible to
    setup -- advertising, reachable, and never offered.

    So the proprietary service UUIDs match in their own right, and the name is
    only a convenience for the panel this was written against. Whether the
    advert can *key* a config entry is a separate question -- see
    :func:`advert_name`.
    """
    if info.name and info.name.startswith(LOCAL_NAME_PREFIX):
        return True
    return not PANEL_SERVICE_UUIDS.isdisjoint(info.service_uuids)


def advert_name(info: BluetoothServiceInfoBleak) -> str | None:
    """The panel's stable advertised name, or ``None`` if it has not given one.

    Home Assistant substitutes the address when an advertisement carries no
    local name -- which the panel's add-device adverts do not. That address is
    a rotating RPA, so keying anything on it produces a fresh, MAC-titled
    discovery every rotation instead of one correctly-named panel. Callers that
    need a key must wait for a named advert; one follows shortly.
    """
    if not info.name or info.name.upper() == info.address.upper():
        return None
    return info.name


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

    The local name is absent from add-device/pairing adverts, so the service
    UUID is an equal-standing match rather than a fallback.
    """
    return [
        info
        for info in bluetooth.async_discovered_service_info(hass, connectable=False)
        if info.name == name or not PANEL_SERVICE_UUIDS.isdisjoint(info.service_uuids)
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
# controller -- the README's proxy section has which kernel does what, and why
# 6.19 changed it -- so it is worth remembering rather than guessing: see
# ``prefer_identity``.
ADDR_IDENTITY = "identity"
ADDR_RPA = "rpa"


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
    # Rank, never remove. Freshest first, but an avoided address sinks below
    # every other candidate and the unwanted kind of address sinks below the
    # wanted one:
    #
    #   fresh→stale wanted | other kind | avoided wanted | avoided other
    candidates = sorted(
        infos,
        key=lambda i: (
            i.address.upper() in avoid_norm,
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
