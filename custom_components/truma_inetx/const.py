"""Constants for the Truma iNet X (BLE) integration."""

from __future__ import annotations

import logging
from collections.abc import Iterable

DOMAIN = "truma_inetx"
LOGGER = logging.getLogger(__package__)

# Advertised local-name prefix used for discovery / manual matching. An iNet X
# Panel 2 still carries it -- "Truma iNetX-5770EB", measured in issue #6 -- so
# it survived a hardware generation that changed the advertised service UUID.
LOCAL_NAME_PREFIX = "Truma iNetX"

# Every Truma BLE UUID shares this base; only the first word varies. The panel
# this integration was written against advertises fc310000 and carries fc310002
# in its GATT table. An iNet X Panel 2 advertises fc310006 instead (issue #6),
# over the same fc314001-fc314004 characteristics.
#
# Matching the base rather than a list of numbers is the point. The list was
# written when nobody had seen the first word move; a Panel 2 moved it, and
# until a release added that one number the panel was heard by the proxy,
# reachable, and never offered -- indistinguishable, from outside, from not
# being heard at all. The base is Truma's own and cannot move without them
# abandoning their own UUID space, so a renumbered panel now needs no release.
TRUMA_UUID_BASE = "-f3b2-11e8-8eb2-f2801f1b9fd1"

# Not matched on, deliberately: Truma's assigned Bluetooth company identifier,
# 0x0c73 (3187, which is how Home Assistant renders it in diagnostics). The
# panel does advertise it -- payload 0000 idle, 0001 in add-device mode -- but
# it says *Truma*, not *panel*, and Truma sells other Bluetooth devices. It is
# still the fastest way to find a panel in a capture by hand.


def has_truma_uuid(service_uuids: Iterable[str]) -> bool:
    """Return True when any advertised UUID belongs to Truma's own space."""
    return any(uuid.lower().endswith(TRUMA_UUID_BASE) for uuid in service_uuids)


def looks_like_panel(name: str | None, service_uuids: Iterable[str]) -> bool:
    """Return True when this advertisement belongs to a Truma panel.

    Either test alone is evidence. They are not interchangeable: the service
    UUIDs ride in the advertisement proper and so survive passive scanning,
    while the local name lives in the scan response and needs an active scan.
    The name is also Truma's to change, and one of the two is all a panel in
    add-device mode may give us.

    Lives here, next to the constants, rather than in ``bt``: ``bus`` needs the
    same answer for its ``dump_bus`` scan and must not import Home Assistant.
    """
    if name and name.startswith(LOCAL_NAME_PREFIX):
        return True
    return has_truma_uuid(service_uuids)


# Manufacturer shown in the HA device registry.
MANUFACTURER = "Truma"
MODEL = "iNet X Panel"

# Repair-issue id raised when the panel is advertising and nothing can connect
# to it. Without it the failure is silent: the resolver only logs at debug
# level and entities sit unavailable with no clue why. It says nothing about
# which kind of adapter is missing -- Home Assistant picks among whatever can
# reach the panel, and the reason none can is the user's to find.
ISSUE_NO_ROUTE = "no_route"
# The host will never ask for a scan response, so no panel name can arrive.
# A different fault from "heard but unreachable", with different advice: this one
# is a scanning-mode/controller fact, not a resolver hiccup.
ISSUE_SCANNER_PASSIVE = "scanner_passive"
# Nothing is being heard from *any* device: the host's radio has stopped, which is
# a different fault from a panel that is switched off -- and the only one of the
# two that is not the user's to fix by walking to the van.
ISSUE_HOST_DEAF = "host_deaf"
# How long the whole host may hear nothing before it is worth a card, and how
# many failed connect rounds must agree. Both generous on purpose: a quiet
# workshop with no other BLE devices nearby must not read as a dead radio.
HOST_DEAF_QUIET_SECONDS = 600.0
HOST_DEAF_MISSES_BEFORE_ISSUE = 3
# A *proxy* scanner that has stopped delivering advertisements. Measured on the
# van (2026-10-26, twice in one afternoon): the ESP32 proxy stays reachable on
# its API and keeps carrying the Renogy links, yet Home Assistant receives no
# advertisements from it at all -- "Bluetooth scanner has gone quiet for
# 1331.8s" -- and nothing recovers on its own. The cure is a reload of that
# proxy's **ESPHome** config entry, which is not the advice the host-wide card
# gives, so the two faults need two cards.
ISSUE_PROXY_QUIET = "proxy_quiet"
# This Home Assistant holds no usable bond for the panel -- the *host* is the
# side that is missing key material, not the panel. Measured on the van
# 2026-10-26 by forging the two host-side states (`--signature-only`, `--drop-irk`):
# both produce HCI 0x0E refusals at the panel and, until this card, the only one
# that spoke said "the panel has most likely forgotten this Home Assistant" and
# sent the user to clear the panel's device list for a defect on this side.
# Raised only on positive absence (`Device1.Paired == False`); True is never
# treated as proof of a complete record, and None (no bus, panel not in BlueZ's
# object list) keeps the refusal card, because an unknown is not an absence.
ISSUE_HOST_BOND_MISSING = "host_bond_missing"
# Shorter than the host-wide figure because this one is a per-scanner signal:
# a registered proxy that has delivered nothing for this long has stalled,
# while other scanners can still be hearing the world perfectly well.
PROXY_QUIET_SECONDS = 180.0
# Two connect rounds, not three: the per-scanner clock above is already the
# evidence, so this only has to outlast a single unlucky sample.
PROXY_QUIET_MISSES_BEFORE_ISSUE = 2
# The id this issue had before it stopped being about proxies. Deleted once at
# startup so an issue raised by an older version does not linger with no
# translation behind it.
ISSUE_NO_PROXY_ROUTE_LEGACY = "no_proxy_route"

# The panel is on air, we hold a bond for it, and every connect is refused.
# Measured on the van (2026-10-25, reproduced on demand by clearing the panel's
# own device list): bluetoothd logs "Connection Rejected Due to Security Reasons"
# (HCI 0x0E) because the panel no longer holds this host's key, while both
# adapters still hold a complete bond. Home Assistant only surfaces
# "Failed to connect after 3 attempt(s): TimeoutError", so the reason is inferred
# from those three facts -- and the user was told nothing at all before this.
ISSUE_PANEL_REFUSING = "panel_refusing"
# Consecutive refused sessions before the card appears. Low, because this state
# never clears by itself: the panel will refuse forever until its list is fixed.
REFUSED_SESSIONS_BEFORE_ISSUE = 3

# A Truma panel is advertising but never supplies a name, which is what a
# passively scanning adapter looks like: the name rides in a scan response and
# only an active scan asks for one. Nothing is wrong with the panel and nothing
# in this integration can fix it, so the state is surfaced -- as a Repairs entry
# that persists, and as one discovery card per run -- instead of being logged at
# a level nobody reads. Deleted by note_named() as soon as a name arrives.
ISSUE_PANEL_UNNAMED = "panel_unnamed"

# The link comes up and the panel's bus answers nothing: every address asked,
# none acknowledged. Measured on the van (2026-10-25): a panel in this state
# stays deaf however often it is dialled, and reconnecting into it every 45 s
# for ten minutes only made it worse; a power cycle cleared it. So stop, say
# so, and let it be.
ISSUE_PANEL_SILENT = "panel_silent"
# Consecutive deaf sessions before the issue is raised and the loop backs off.
SILENT_SESSIONS_BEFORE_ISSUE = 3
# How long to leave a deaf panel alone. Long enough to be a real pause, short
# enough that the next poll notices if it comes back by itself.
SILENT_BACKOFF = 1800.0
# Consecutive failed resolves before raising that issue. A single miss is
# normal -- an adapter can be busy mid-connect, or the panel between
# advertising intervals -- so warning on the first one would cry wolf.
NO_ROUTE_MISSES_BEFORE_WARNING = 3


def _mode_name(mode: object) -> str | None:
    """``BluetoothScanningMode`` as the word a person would say."""
    if mode is None:
        return None
    return (getattr(mode, "name", None) or str(mode)).lower()


def scan_mode_report(hass) -> list[dict[str, object]]:
    """What each scanner is asked to do, and what it is doing right now.

    Two different questions, and answering the wrong one is how a warning cries
    wolf: ``requested_mode`` is the configured mode, ``current_mode`` is the live
    one. On the default AUTO the radio is *passive* almost all the time and turns
    active only for scheduled windows, so ``current=PASSIVE`` is normal. Only
    ``requested=PASSIVE`` means no window will ever open.

    Tolerant by construction: an older Home Assistant without the accessor, or a
    scanner without the attributes, yields an empty report rather than an error.
    """
    try:
        from homeassistant.components.bluetooth import async_current_scanners

        scanners = async_current_scanners(hass)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        LOGGER.debug("Truma: cannot read the scanner modes: %s", exc)
        return []
    report: list[dict[str, object]] = []
    for scanner in scanners or ():
        requested = _mode_name(getattr(scanner, "requested_mode", None))
        current = _mode_name(getattr(scanner, "current_mode", None))
        report.append(
            {
                "scanner": getattr(scanner, "name", None)
                or getattr(scanner, "source", "?"),
                "source": getattr(scanner, "source", None),
                "requested": requested,
                "current": current,
                "connectable": bool(getattr(scanner, "connectable", False)),
            }
        )
    return report


def scanners_that_never_scan_active(hass) -> list[str]:
    """Scanners whose *requested* mode is passive: no active window will ever open.

    ``requested_mode`` is the configuration, ``current_mode`` is the moment -- on
    the default AUTO the radio is passive almost all the time and turns active
    only for scheduled windows, so ``current=passive`` proves nothing. Only a
    requested mode of passive means a *scan response* can never arrive, and a
    scan response is the only place the panel's name travels (issue #31).
    """
    names: list[str] = []
    for entry in scan_mode_report(hass):
        if str(entry.get("requested") or "").upper() == "PASSIVE":
            names.append(str(entry.get("scanner") or "a scanner"))
    return names


def quiet_proxy_scanners(hass, quieter_than: float = PROXY_QUIET_SECONDS) -> list[str]:
    """Proxies that have delivered nothing for a while, by name.

    A remote scanner (an ESPHome Bluetooth proxy) is the one transport this
    integration cannot see into: its radio, its bond and its advertisement
    stream all live on the far side of an API. Measured on the van 2026-10-26:
    the proxy kept answering its own API and kept carrying other integrations'
    links while Home Assistant received no advertisements from it at all, and
    reloading its **ESPHome** config entry was the only thing that brought the
    stream back. That is a different cure from the host-wide advice, so it
    needs its own question -- answered from each scanner's own silence, which
    is why this cannot be read off the device cache (a host that hears nothing
    still holds yesterday's entries there).

    ``isinstance(scanner, BaseHaRemoteScanner)`` is the discriminator rather
    than a name or module test: it is the class habluetooth itself uses for
    every proxy, so a renamed proxy cannot slip past it. Tolerant by
    construction -- an older Home Assistant, or a scanner without the method,
    yields an empty list rather than an error.
    """
    try:
        from habluetooth import BaseHaRemoteScanner
        from homeassistant.components.bluetooth import async_current_scanners

        scanners = async_current_scanners(hass)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
        LOGGER.debug("Truma: cannot read the proxy scanners: %s", exc)
        return []
    names: list[str] = []
    for scanner in scanners or ():
        if not isinstance(scanner, BaseHaRemoteScanner):
            continue
        try:
            quiet = scanner.time_since_last_detection()
        except Exception:  # noqa: BLE001 - no detection yet is not an answer
            continue
        if quiet is None or quiet < quieter_than:
            continue
        names.append(str(getattr(scanner, "name", None) or getattr(scanner, "source", "a proxy")))
    return names


def describe_scan_modes(hass) -> str:
    """One line naming which scanners can ever produce a name, and why not."""
    report = scan_mode_report(hass)
    if not report:
        return "no Bluetooth scanners are registered"
    parts: list[str] = []
    for entry in report:
        requested = str(entry.get("requested") or "unknown")
        current = str(entry.get("current") or "starting")
        line = f"{entry.get('scanner')} requested={requested} current={current}"
        if requested == "passive":
            line += (
                ": pinned passive, so it never opens an active window and no"
                " name can arrive from it"
            )
        elif requested == "auto":
            line += ": auto, passive except for short scheduled windows"
        parts.append(line)
    return "; ".join(parts)
