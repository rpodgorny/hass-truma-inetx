# Credits, licensing and artwork

[← README](../README.md)

The Home Assistant integration — coordinator, BLE transport, pairing, config
flow and all entity platforms — is original work in this repository, licensed
under **GPL-3.0** (see [LICENSE](../LICENSE)).

The wire protocol implementation in `custom_components/truma_inetx/truma/`
(`protocol.py`, `const.py`) is **vendored from
[daaaaan/truma-inetx-ble](https://github.com/daaaaan/truma-inetx-ble)**, whose
reverse-engineering of the iNet X protocol made this integration possible. That
project publishes no licence, so its author retains all rights and the GPL-3.0
above does **not** apply to those files. They are isolated in their own
subpackage so the boundary stays visible; if upstream adds a licence and ships
an installable package, the subpackage will be replaced by a dependency.

`protocol.py` is vendored unchanged. `const.py` carries local additions on top:
the extra device seeds and topics parameter discovery walks, and the
measure-request constants. Those additions are original work, but they sit in a
file whose base is not, so the licence position above governs the file as a
whole.

`state.py` used to sit there too and no longer exists: the model it held — one
flat `Topic.Param` dict and a table of typed fields — assumed every topic has a
single owner, which a bus does not. What replaced it is `bus.py`, written from
the frames on the wire, so it is original work and sits outside the quarantine
under GPL-3.0 with the rest.

## Icon

The integration ships its own artwork in `custom_components/truma_inetx/brand/`
(`icon.png` 256×256, `icon@2x.png` 512×512) — the Truma iNet X system mark, with
the "iNet X" wordmark removed and the mark re-centred. It is **Truma's
trademark, not covered by this repository's GPL-3.0 licence**; see
[`brand/ATTRIBUTION.md`](../custom_components/truma_inetx/brand/ATTRIBUTION.md)
for the source, what was changed and the trademark notice. Since
[Home Assistant 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
these are served straight from the integration through HA's brands proxy and
take priority over the brands CDN — no submission to
[home-assistant/brands](https://github.com/home-assistant/brands) and no
manifest entry required.

On Home Assistant older than 2026.3 the UI falls back to a default icon. The
HACS store listing may also still show a placeholder, since it fetches icons
from the HACS CDN rather than from the repository
([hacs/integration#5223](https://github.com/hacs/integration/issues/5223)).

## Trademark

"Truma" and the Truma iNet X mark are trademarks of Truma Gerätetechnik GmbH &
Co. KG. This project is not affiliated with, endorsed, sponsored by or supported
by Truma.
