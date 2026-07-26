# Truma iNet X (BLE) — Home Assistant integration

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
| Truma iNet X | `climate` | Off / Heat / Fan-only, target temperature, fan level |
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

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/rpodgorny/hass-truma-inetx`, category **Integration**
3. Install **Truma iNet X (BLE)**, then restart Home Assistant

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

- **The panel's Bluetooth device list is full.** It stores only ~4 devices and
  silently rejects new bonds when full. Clear the list in the app and retry.
- **Still failing → power-cycle the panel.** It then advertises a fresh address
  that pairs cleanly. This also clears "ghost" connections holding a slot.

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

Not affiliated with or endorsed by Truma Gerätetechnik GmbH & Co. KG.
