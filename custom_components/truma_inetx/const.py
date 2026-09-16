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
# The id this issue had before it stopped being about proxies. Deleted once at
# startup so an issue raised by an older version does not linger with no
# translation behind it.
ISSUE_NO_PROXY_ROUTE_LEGACY = "no_proxy_route"
# Consecutive failed resolves before raising that issue. A single miss is
# normal -- an adapter can be busy mid-connect, or the panel between
# advertising intervals -- so warning on the first one would cry wolf.
NO_ROUTE_MISSES_BEFORE_WARNING = 3
