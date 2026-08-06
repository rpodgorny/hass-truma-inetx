"""Shared Bluetooth resolution for the Truma iNet X panel.

The panel uses a rotating Resolvable Private Address, so a stored MAC goes
stale. Both the live session (coordinator) and the onboarding bond (config
flow) must find and connect the panel THE SAME way — through a remote/proxy
scanner — so the bond lands where the integration will actually connect. This
module is the single source of that resolution.
"""

from __future__ import annotations

from collections.abc import Container

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import LOGGER
from .truma.const import SERVICE_UUID


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
        if info.name == name or SERVICE_UUID in info.service_uuids
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
    hass: HomeAssistant, name: str, *, avoid: Container[str] = ()
) -> BLEDevice | None:
    """Find the panel's current connectable device via a remote/proxy scanner.

    Matches the panel by its stable advertised ``name`` OR primary service UUID
    (the local name is absent from add-device/pairing adverts), picks the
    freshest advertised RPA, and returns ONLY a device reachable through a
    remote (proxy) scanner — local host adapters cannot maintain a rotating-RPA
    link (BlueZ pairs but can't reconnect) and would steal the connection from
    the proxy. Returns ``None`` when no proxy route is available right now (the
    caller should retry).

    The "resolved identity" pseudo-address (whose last bytes match the name
    suffix, e.g. ``...FFB4D1``) is excluded — connecting it dials a stale cached
    bonded RPA rather than the live one.

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
    LOGGER.debug(
        "Truma %s candidates (fresh→stale RPAs): %s | avoid: %s | identity present: %s",
        name,
        [(i.address, round(i.time, 1), i.rssi, i.connectable) for i in rpas],
        sorted(avoid_norm),
        any(_is_identity(i.address) for i in infos),
    )
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
    LOGGER.debug("Truma %s: no proxy route to the panel right now", name)
    return None
