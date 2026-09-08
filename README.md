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

## An ESP32 Bluetooth proxy is the reliable route

A proxy works everywhere. A local adapter works on some kernels and not
others, and which one you are on decides it.

The panel advertises a **fast-rotating Resolvable Private Address** and only
accepts an encrypted reconnect from a client that puts that current address on
air. Phones resolve it in the Bluetooth controller, and ESP-IDF does the same,
so an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
always works.

On Linux it depends on the kernel version:

- **Below 6.19** a local adapter reconnects fine. `hci_connect_le()`
  substitutes the peer's cached RPA for the identity address before it puts a
  connection on air, so no LL Privacy and no proxy is needed. Kernel 6.12, the
  one the Pi 5 in [#13](https://github.com/rpodgorny/hass-truma-inetx/issues/13)
  runs, is in this range, and `14b06c3a88f7` has not been backported to any
  6.12, 6.17 or 6.18 stable release.
- **6.19 and later** it usually does not. Commit
  [`14b06c3a88f7`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=14b06c3a88f7)
  made the kernel keep the identity address all the way to the controller, so
  the panel never hears the connect. A local adapter then only works if the
  controller supports LL Privacy *and* BlueZ has programmed the panel's IRK
  into its resolving list, which currently does not happen for dual-mode bonds
  ([bluez#2356](https://github.com/bluez/bluez/issues/2356)).

A kernel fix is
[posted to linux-bluetooth](https://lore.kernel.org/linux-bluetooth/20260908012048.3681904-2-radek@podgorny.cz/)
and is working its way upstream. Until it lands, a proxy is the answer on 6.19
and later.

Older reports in this repo claim BlueZ can never do this. That was wrong: the
adapters tested happened to be on kernels carrying the regression.

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

If the integration can hear the panel advertising but cannot connect to it
repeatedly, it raises an issue under Settings → **Repairs** saying so, rather
than leaving the entities unavailable with no explanation. It stays quiet while
the panel is simply switched off or out of range — that is not the same fault —
and clears the issue on the next successful connect.

## Entities

| Entity | Platform | Notes |
|---|---|---|
| Truma iNet X | `climate` | Whichever modes the panel offers — Off / Heat / Fan-only everywhere, plus Auto, Cool or Dry where the vehicle has them. 5–30 °C in 1 °C steps. Offers only the control the current mode uses: the target temperature while heating, the fan speed as the fan mode (`off`, `1`–`10`) while venting |
| Room temperature | `sensor` | °C |
| Water temperature | `sensor` | °C |
| Internal temperature | `sensor` | °C |
| Supply voltage | `sensor` | V |
| Water heating | `select` | Off / Eco (40 °C) / Comfort (60 °C) / Hot (70 °C) — the steps the panel offers |
| Electric heating | `select` | Supplemental electric element: off / 900 W / 1800 W — the steps the panel offers. Only where the vehicle has the element |
| Diesel burner | `switch` | Only where the heater has a diesel burner |
| Gas | `binary_sensor` | Whether the heater is drawing on gas. Read-only — the heater moves this itself. Only where it burns gas |
| Fan level | `number` | 0–10 |
| Flame | `binary_sensor` | Burner currently firing |
| BLE connection | `binary_sensor` | Diagnostic — is the panel connected |
| Fresh water | `sensor` | % — only where the vehicle has a tank sensor |
| Grey water | `sensor` | % — only where the vehicle has a tank sensor |
| Fresh water pump | `switch` | Only where the vehicle has one |
| Starter battery | `sensor` | V — only where something reports `VBat.Voltage` |
| Leisure battery | `sensor` | V — only where something reports `L1Bat.Voltage` |
| Flame status | `sensor` | Diagnostic, disabled by default — the raw `System.FlameStatus` value |

The climate entity's mode list and the two selects' options are not fixed. The
panel enumerates each parameter for the vehicle it is installed in — a van with no air conditioner
does not list a cooling mode, and a heater without the electric element does
not list 1800 W — so the entities offer what the panel offers, falling back to
the full list where it describes nothing. The panel's own names for the values
are never shown: they arrive in the panel's display language, and the labels
here stay translatable.

Everything marked "only where" is created the first time the hardware behind it
reports a value, rather than up front: vehicles differ far more than the
protocol does — a Combi D has no electric element and its panel never mentions
the parameter, a gas/electric Combi has no diesel burner, most vans have no
tanks and no electrical block — and an entity that is permanently unknown
because the hardware does not exist looks exactly like one that is unknown
because the integration is broken.

Gas is deliberately a sensor and not a switch. `EnergySrc.GasLevel` is
writable and the write does go through, but the heater writes it too: on a
gas/electric Combi, switching the electric element off was measured turning the
gas source on by itself. A control over something the appliance also drives
would fight it and flap, so the reading reflects the heater's choice rather
than pretending to make it.

The flame status sensor exists because nothing published says what
`System.FlameStatus` means. It takes 0, 1 and 2; the binary sensor above has to
answer on or off, and does it by treating anything non-zero as lit. The panel
describes the parameter with the same type code it gives `AirCirculation.Active`
and the other `Active` fields, which are the protocol's OFF / ACTIVE / IDLE
triple — good evidence, not proof. Watching the raw value through an ignition
is what would settle it.

The water select's options changed in 0.7.1b4, from `40 °C / 60 °C / 70 °C` to
`Eco (40 °C) / Comfort (60 °C) / Hot (70 °C)`, so that the name matches what the
panel writes on the vehicle and the temperature says what the name means. An
automation or script that calls `select.select_option` with one of the old
strings has to be updated; the values on the wire are unchanged.

On a vehicle that already had the electric select or the diesel switch before
they became conditional, Home Assistant keeps the old entity in its registry
and shows it as unavailable. Deleting it once from the device page is the only
cleanup; the integration does not remove entities by itself, because a
parameter that has not been reported *yet* is not the same as hardware that
does not exist.

Updates are pushed as the panel sends them (roughly 25 frames/minute), not
polled. The tank levels are the exception. A tank sensor answers with the
level it measured when it was last *asked*, and nothing on the bus asks it
except the panel, when its water screen is opened — so a tank emptied by hand
would otherwise keep reporting its old level indefinitely. The integration
asks for a fresh measurement once per connect and every 60 s while the link is
held, addressed to whichever device reported the tank.

The panel drives its own fan while heating and has no setpoint at all while
venting, so exactly one of the two controls is meaningful at any time. The
climate entity reflects that: `supported_features` follows the mode rather than
advertising both at once. Off keeps the setpoint, the way every other
thermostat in Home Assistant does — it is the resting target you come back to.
The `number` entity exposes the fan level in every mode, for automations.

## Dashboard card

The integration ships a thermostat card whose dial follows the mode: it sets
the temperature while heating and the fan speed while venting, and is disabled
while off. Home Assistant's own climate dial is bound to temperature and
humidity only, and climate fan modes are arbitrary strings rather than a
numeric range, so core cannot put fan speed on an arc.

**There is nothing to install.** The integration serves the card at
`/truma_inetx/truma-climate-dial-card.js` and registers it with the frontend,
so it arrives and updates with the integration. Just add it to a dashboard:

```yaml
type: custom:truma-climate-dial-card
entity: climate.truma_inetx_ffb4d1
name: Heating          # optional
```

A HACS repository belongs to exactly one category, so this repository cannot
also be published as a HACS *plugin*. Serving the card from the integration
avoids a second repository to version and tag. The integration version is
appended to the URL as a query string, because the frontend service worker
caches assets for weeks and a browser hard-refresh does not bypass it — without
a changing URL an updated card would never reach the browser.

The trade-off: `add_extra_js_url` loads the module on every page load for every
user, not only when the card is on screen. It is about 19 KB.

The card does not reimplement the dial — it instantiates Home Assistant's own
`ha-control-circular-slider` and `ha-outlined-icon-button` and reuses the
frontend's layout CSS, so it inherits upstream's appearance and behaviour.
Those are internal frontend components with no stability guarantee: upstream
restyling arrives for free, an upstream rename breaks the card (it then renders
an explicit error naming the missing component).

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

## Diagnostics

The device page's ⋮ → **Download diagnostics** dumps the config entry, whether
the last update succeeded, and the full decoded panel state — including
`seen_devices`, every bus address the integration has heard from, which is the
evidence for what is actually on a given vehicle's bus.

The state also carries `param_meta`: what the panel says each parameter *is*,
as opposed to what it currently reads — its range, whether it can be written,
and for an enum the panel's own name for every value, with the ones this
vehicle cannot produce marked. Truma documents none of the protocol, but the
panel describes it in every frame, so a download answers "what does this value
mean" without anyone having to watch their heater and write it down. The same
descriptions are logged, once each, at debug level.

The BLE address, the panel's name and the persisted app identity (`muid` /
`uuid`) are redacted: the address is a private address that still pins the panel
to a location, and the identity is what the panel bonds against. The panel state
itself carries nothing identifying.

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

The checks in `tests/` are self-contained. They stub Home Assistant, bleak and
dbus, so they need neither an HA install nor hardware, and each file is a
script — run one directly, or all of them:

```bash
python3 tests/test_pairing_rotation.py            # pairing address rotation
python3 tests/test_pairing_transport_dispatch.py  # bonding uses the transport it has
python3 tests/test_device_from_bluez.py           # BLEDevice built from BlueZ's object
python3 tests/test_no_proxy_issue.py              # the "nothing can reach it" repair
python3 tests/test_water_entities.py              # water entities and write addressing
python3 tests/test_energy_entities.py             # energy sources, batteries, raw flame value
```

The remaining five drive real code that imports a library, so they need it
installed — `voluptuous` for the config flow's schema, `cbor2` for the four
that reach the protocol module, whether to build real frames and parse them back
or by way of the coordinator that imports it:

```bash
pip install voluptuous
python3 tests/test_panel2_discovery.py            # a renamed panel is still offered

pip install cbor2==5.6.5
python3 tests/test_param_discovery.py             # startup registration + discovery
python3 tests/test_measure_request.py             # asking the tanks to measure
python3 tests/test_param_meta.py                  # what the panel says a value means
python3 tests/test_panel_declared_options.py      # offering what the panel says exists
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
GPL-3.0 above does **not** apply to those files. They are isolated in their own
subpackage so the boundary stays visible; if upstream adds a licence and ships
an installable package, that subpackage will be replaced by a dependency.

`protocol.py` is vendored unchanged. `state.py` and `const.py` carry local
additions on top of the vendored code: the fresh-water pump and the two tank
levels, `seen_devices` and `topic_source` (which bus device reported a topic),
the extra device seeds and topics that parameter discovery walks, and the
measure-request constants. Those additions are original work in this
repository, but they sit inside files whose base is not, so the licence
position above governs the files as a whole.

The integration icon is the Truma iNet X system mark, used descriptively to
identify the device this integration talks to — see
[`brand/ATTRIBUTION.md`](custom_components/truma_inetx/brand/ATTRIBUTION.md).
It is excluded from the GPL-3.0 licence above.

"Truma" and the Truma iNet X mark are trademarks of Truma Gerätetechnik GmbH &
Co. KG. This project is not affiliated with, endorsed, sponsored by or
supported by Truma.

