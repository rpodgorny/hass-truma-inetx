# Truma iNet X (BLE) — Home Assistant integration

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml/badge.svg)](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Local push integration for the **Truma iNet X** control panel over Bluetooth LE.
Reads room/water/internal temperatures and supply voltage, and controls heating
mode, target temperature, water heating, electric heating level, the diesel
burner and the fan — no cloud, no Truma account, no LIN wiring.

Developed against an iNet X driving a **Truma Combi**. Other Truma appliances
speak the same protocol but are untested; reports welcome.

## ⚠️ An ESP32 Bluetooth proxy is required

This is not a preference — it is the only configuration that works.

The panel advertises a **fast-rotating Resolvable Private Address** and only
accepts an encrypted reconnect from a client that can resolve that address back
to the bond. Phones do this in the Bluetooth controller. BlueZ on Linux
(Raspberry Pi, x86, any adapter tried — onboard BCM, CSR, RTL8761B) does not:
it can *pair* the panel, but every later reconnect lands on an address it cannot
map to the stored key, so the link is dropped. That was verified exhaustively —
IRK stored, LL-Privacy enabled, `Experimental` flags, three adapters — and it
still fails at the controller level.

ESP-IDF resolves RPAs in-controller like a phone does, so an
[ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
works where the host adapter cannot.

**Stock proxy firmware is enough** — nothing custom is needed. A plain
`bluetooth_proxy: active: true` on an `esp-idf` build is all this integration
expects:

```yaml
esp32:
  framework:
    type: esp-idf   # required: more connection slots + in-controller RPA resolution

bluetooth_proxy:
  active: true
```

Put the proxy **within a few metres of the panel**. Distance shows up as
`ESP_GATT_CONN_FAIL_ESTABLISH` connect failures rather than as a clean error.

## Entities

| Entity | Platform | Notes |
|---|---|---|
| Truma iNet X | `climate` | Off / Heat / Fan-only. Offers only the control the current mode uses: the target temperature while heating, the fan speed as the fan mode (`off`, `1`–`10`) while venting |
| Room temperature | `sensor` | °C |
| Water temperature | `sensor` | °C |
| Internal temperature | `sensor` | °C |
| Supply voltage | `sensor` | V |
| Water heating | `select` | Off / Eco / High / Boost |
| Electric heating | `select` | Electric heating element level |
| Diesel burner | `switch` | |
| Fan level | `number` | 0–10 |
| Flame | `binary_sensor` | Burner currently firing |
| BLE connection | `binary_sensor` | Diagnostic — is the panel connected |

Updates are pushed as the panel sends them (roughly 25 frames/minute), not
polled.

The panel drives its own fan while heating and has no setpoint at all while
venting, so exactly one of the two controls is meaningful at any time. The
climate entity reflects that: `supported_features` follows the mode rather than
advertising both at once.

## Optional dashboard card

`lovelace/truma-climate-dial-card.js` is a thermostat card whose dial follows
the mode: it sets the temperature while heating and the fan speed while
venting, and is disabled while off. Home Assistant's own climate dial is bound
to temperature and humidity only, and climate fan modes are arbitrary strings
rather than a numeric range, so core cannot put fan speed on an arc.

The card does not reimplement the dial — it instantiates Home Assistant's own
`ha-control-circular-slider` and `ha-outlined-icon-button` and reuses the
frontend's layout CSS, so it inherits upstream's appearance and behaviour.
Those are internal frontend components with no stability guarantee: upstream
restyling arrives for free, an upstream rename breaks the card (it then renders
an explicit error naming the missing component).

Install:

1. Copy the file to `<config>/www/`.
2. Settings → Dashboards → ⋮ → Resources → add
   `/local/truma-climate-dial-card.js` as a **JavaScript module**.
3. Add to a dashboard:

   ```yaml
   type: custom:truma-climate-dial-card
   entity: climate.truma_inetx_ffb4d1
   ```

When updating the file, bump a `?v=` query on the resource URL. Home
Assistant's frontend service worker caches `/local/` aggressively, and a
browser hard-refresh does not bypass it.

## Installation

### HACS (custom repository)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rpodgorny&repository=hass-truma-inetx&category=integration)

Or manually:

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/rpodgorny/hass-truma-inetx`, category **Integration**
3. Install **Truma iNet X (BLE)**, then restart Home Assistant
4. Settings → Devices & Services → the panel should be discovered; see
   [Pairing](#pairing)

### Icon

The integration ships its own artwork in `custom_components/truma_inetx/brand/`
(`icon.png` 256×256, `icon@2x.png` 512×512) — the Truma iNet X system mark,
with the "iNet X" wordmark removed and the mark re-centred. It is **Truma's
trademark, not covered by this repository's GPL-3.0 licence**; see
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md)
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

### Manual

Copy `custom_components/truma_inetx/` into your Home Assistant `config/custom_components/`
directory and restart.

## Pairing

The panel uses **Just Works** pairing (no passkey is shown) and only bonds while
it is actively in add-device mode. It is genuinely finicky — these rules matter:

1. Put the panel **freshly** into add-device mode (Truma iNet X app, or on the
   panel itself) so its pairing screen is up.
2. In Home Assistant the panel should appear as a discovered device. Otherwise
   Settings → Devices & Services → **+ Add Integration** → *Truma iNet X (BLE)*.
3. Press **Submit once.** Repeated submits against a panel that is not cleanly
   ready make it show "something went wrong" and it then needs re-arming.

Pairing normally completes in a few seconds.

### If pairing fails

Work through these in order — always re-entering add-device mode before each
attempt, since the panel only accepts a bond while its pairing screen is up:

1. **Clear the panel's saved Bluetooth device list.** It stores only ~4 devices
   and silently rejects new bonds once full. Clear it in the Truma iNet X app,
   re-arm add-device mode, and try again.
2. **If clearing the list did not help, power-cycle the panel and start over.**
   Switch it off and on, put it back into add-device mode, and repeat the whole
   pairing step. This drops "ghost" connections that hold one of the panel's
   connection slots, and makes it advertise a fresh Bluetooth address that
   pairs cleanly. It resolves most stubborn cases.

You do **not** need to clear any bonds on the Bluetooth proxy. If the proxy
still holds a bond the panel has forgotten, the panel rejects it on that one
address only (`error: 97`), and the integration rotates to the panel's next
address, which pairs normally.

To re-pair later, use **Reconfigure** on the device.

## Known limitations

- **Reconnects can wedge.** If the link drops, reconnecting to the same address
  sometimes fails repeatedly with `ESP_GATT_CONN_FAIL_ESTABLISH` (0x3e). The
  integration backs off and rotates between the panel's advertised addresses,
  which usually recovers it; occasionally a panel power-cycle is needed. Under
  investigation.
- **Duplicate entries in the panel's device list.** Each pairing can leave an
  extra record. Harmless so far, but it consumes the panel's ~4 slots.
- Only the local name / service UUID are used for discovery; the stored address
  is treated as volatile because it rotates.

## Development

`tests/test_pairing_rotation.py` is a self-contained check of the pairing
address-rotation logic. It stubs Home Assistant, bleak and dbus, so it needs
neither an HA install nor hardware:

```bash
python3 tests/test_pairing_rotation.py
```

## Credits and licensing

The Home Assistant integration — coordinator, BLE transport, pairing, config
flow and all entity platforms — is original work in this repository and is
licensed under **GPL-3.0** (see [LICENSE](LICENSE)).

The wire protocol implementation in `custom_components/truma_inetx/truma/`
(`protocol.py`, `state.py`, `const.py`) is **vendored from
[daaaaan/truma-inetx-ble](https://github.com/daaaaan/truma-inetx-ble)**, whose
reverse-engineering of the iNet X protocol made this integration possible.
That project publishes no licence, so its author retains all rights and the
GPL-3.0 above does **not** apply to those files. They are kept unmodified and
isolated in their own subpackage; if upstream adds a licence and ships an
installable package, that subpackage will be replaced by a dependency.

The integration icon is the Truma iNet X system mark, used descriptively to
identify the device this integration talks to — see
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md).
It is excluded from the GPL-3.0 licence above.

"Truma" and the Truma iNet X mark are trademarks of Truma Gerätetechnik GmbH &
Co. KG. This project is not affiliated with, endorsed, sponsored by or
supported by Truma.

