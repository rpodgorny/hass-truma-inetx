"""Shared Bluetooth resolution for the Truma iNet X panel.

The panel uses a rotating Resolvable Private Address, so a stored MAC goes
stale. Both the live session (coordinator) and the onboarding bond (config
flow) must find and connect the panel THE SAME way — through a remote/proxy
scanner — so the bond lands where the integration will actually connect. This
module is the single source of that resolution.
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

# Everything the panel puts on air that identifies it as a Truma panel. Both
# UUIDs are proprietary to Truma, so either one matching is evidence on its
# own -- no local name required.
PANEL_SERVICE_UUIDS = frozenset({ADVERT_SERVICE_UUID, SERVICE_UUID})

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
    """Return True for a remote (e.g. ESP32 proxy) scanner, not a local adapter."""
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

    Separates the two reasons :func:`async_resolve_proxy_device` returns
    ``None``: we hear the panel but nothing connectable can reach it (needs a
    proxy), versus we hear nothing at all (panel off, out of range, or asleep).
    They are indistinguishable to the resolver and need opposite advice, so
    only the first should ever tell a user to go buy hardware.
    """
    return bool(_panel_infos(hass, name))


def async_resolve_proxy_device(
    hass: HomeAssistant,
    name: str,
    *,
    avoid: Iterable[str] = (),
    remote_only: bool = False,
) -> BLEDevice | None:
    """Find the panel's current connectable device via a remote/proxy scanner.

    Matches the panel by its stable advertised ``name`` OR primary service UUID
    (the local name is absent from add-device/pairing adverts), picks the
    freshest advertised RPA, and prefers a device reachable through a remote
    (proxy) scanner: a proxy's controller resolves private addresses itself, so
    it reconnects on any host, and using it also keeps a local adapter from
    stealing the connection. A local adapter is returned only when no proxy can
    hear the panel — on a stock host that link will pair but never reconnect,
    which is what the ``no_proxy_route`` repair issue is about. Returns ``None``
    when nothing can reach it right now (the caller should retry).

    Because of that local fallback, a non-``None`` result does **not** mean a
    proxy can reach the panel. Pass ``remote_only=True`` to ask that narrower
    question -- callers that must know which transport they got, rather than
    just wanting the best route, have to. ``pairing.ensure_bonded()`` does: the
    proxy and local BlueZ bonding paths are not interchangeable, because only
    the local one registers a BlueZ pairing agent.

    The "resolved identity" pseudo-address (whose last bytes match the name
    suffix, e.g. ``...FFB4D1``) is ranked last — via a proxy it usually dials a
    stale cached bonded RPA rather than the live one, but it is the panel's
    real on-air address while in add-device mode, so it is worth a try once the
    RPAs are exhausted.

    ``avoid`` is a set of RPA addresses to skip. This exists because of a
    specific, observed failure mode after pairing:

    Right after the bond, the panel keeps *advertising* the RPA it paired on
    but stops *accepting connections* on it (a panel-side phantom from the
    pairing hand-off), while it simultaneously advertises a fresh, live RPA.
    Both addresses look equally valid here — same name, both connectable via the
    proxy, near-identical timestamps — but connecting the dead one fails
    forever with ESP_GATT_CONN_FAIL_ESTABLISH (0x3e). Without ``avoid`` we
    always return the same first candidate and the coordinator hammers the dead
    address indefinitely, leaving the device "unavailable" until someone
    power-cycles the panel. The coordinator feeds back each address that failed
    to establish so we rotate to the panel's other advertised RPA instead.
    """
    infos = _panel_infos(hass, name)
    suffix = name.rsplit("-", 1)[-1].upper()
    if len(suffix) != 6 or any(c not in "0123456789ABCDEF" for c in suffix):
        suffix = ""

    def _is_identity(address: str) -> bool:
        return bool(suffix) and address.replace(":", "").upper().endswith(suffix)

    avoid_norm = {a.upper() for a in avoid}
    rpas = [i for i in infos if not _is_identity(i.address)]
    rpas.sort(key=lambda i: i.time, reverse=True)
    # The panel also advertises its identity address at times (measured: both at
    # once after bonding, identity only while in add-device mode). When it does,
    # the identity IS its on-air address and connecting to it is correct -- so
    # keep it as a last resort rather than refusing to connect at all.
    idents = [i for i in infos if _is_identity(i.address)]
    idents.sort(key=lambda i: i.time, reverse=True)
    rpas = rpas + idents
    LOGGER.debug(
        "Truma %s candidates (fresh→stale RPAs): %s | avoid: %s | identity present: %s",
        name,
        [(i.address, round(i.time, 1), i.rssi, i.connectable) for i in rpas],
        sorted(avoid_norm),
        any(_is_identity(i.address) for i in infos),
    )
    # A proxy is still preferred: its controller resolves private addresses, so
    # it works on any host. A local adapter only works where the kernel puts the
    # peer's current RPA on air rather than its identity address -- stock Linux
    # does not, see DUCATO_STATE.md. Fall back to one anyway so a patched host
    # can run without a proxy at all.
    local: object | None = None
    for info in rpas:
        # Skip an address the coordinator has told us won't establish (the
        # post-pairing phantom RPA described above), so we try the next one.
        if info.address.upper() in avoid_norm:
            continue
        for sd in bluetooth.async_scanner_devices_by_address(
            hass, info.address, connectable=True
        ):
            if is_remote_scanner(sd.scanner):
                LOGGER.debug(
                    "Truma %s -> %s via remote/proxy scanner (rssi=%s)",
                    name,
                    info.address,
                    getattr(sd.advertisement, "rssi", None),
                )
                return sd.ble_device
            if local is None:
                local = (info, sd)
    if local is not None and not remote_only:
        info, sd = local
        LOGGER.debug(
            "Truma %s -> %s via LOCAL adapter (rssi=%s); no proxy route available",
            name,
            info.address,
            getattr(sd.advertisement, "rssi", None),
        )
        return sd.ble_device
    LOGGER.debug("Truma %s: no route to the panel right now", name)
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
