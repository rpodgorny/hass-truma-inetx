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
