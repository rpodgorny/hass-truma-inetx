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

# Repair-issue id raised when the panel is advertising but no Bluetooth proxy
# can reach it -- the ESP-less setup this integration cannot serve. Without it
# the failure is silent: the resolver only logs at debug level and entities sit
# unavailable with no clue why.
ISSUE_NO_PROXY_ROUTE = "no_proxy_route"
# Consecutive failed resolves before raising that issue. A single miss is
# normal -- the proxy can be busy mid-connect, or the panel between advertising
# intervals -- so warning on the first one would cry wolf constantly.
NO_PROXY_MISSES_BEFORE_WARNING = 3
