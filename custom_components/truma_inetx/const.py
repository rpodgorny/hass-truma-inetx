"""Constants for the Truma iNet X (BLE) integration."""

from __future__ import annotations

import logging

DOMAIN = "truma_inetx"
LOGGER = logging.getLogger(__package__)

# Advertised local-name prefix used for discovery / manual matching.
LOCAL_NAME_PREFIX = "Truma iNetX"

# Manufacturer shown in the HA device registry.
MANUFACTURER = "Truma"
MODEL = "iNet X (Combi)"

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
