"""Shared Bluetooth resolution for the Truma iNet X panel.

The panel uses a rotating Resolvable Private Address, so a stored MAC goes
stale. Both the live session (coordinator) and the onboarding bond (config
flow) must find and connect the panel THE SAME way — through a remote/proxy
scanner — so the bond lands where the integration will actually connect. This
module is the single source of that resolution.
"""

from __future__ import annotations

from collections.abc import Iterable

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import LOGGER
from .truma.const import SERVICE_UUID

# What the panel actually puts in its advertisement. SERVICE_UUID above is the
# GATT service, only visible after connecting, so it never matches an advert.
# Without this the panel can only be recognised by its local name, which lives
# in the scan response and so requires ACTIVE scanning -- under passive scanning
# the panel is invisible even though the radio hears it perfectly.
ADVERT_SERVICE_UUID = "fc310000-f3b2-11e8-8eb2-f2801f1b9fd1"


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
        if info.name == name
        or SERVICE_UUID in info.service_uuids
        or ADVERT_SERVICE_UUID in info.service_uuids
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
    hass: HomeAssistant, name: str, *, avoid: Iterable[str] = ()
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
    if local is not None:
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
