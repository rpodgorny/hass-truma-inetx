# Bluetooth adapter pinning — upstream links

This file collects the upstream Home Assistant / `habluetooth` issues and PRs about pinning a BLE device to a specific adapter (hci0, hci1, ESPHome proxy, etc.). The capability does not exist in HA today — these are the tracking items.

## Core ask
**Optional per-device pin to a scanner/proxy**, with dynamic fallback remaining the default. This is what would let an integration (or user config) say "this Truma panel always uses hci0" or "this mower always uses proxy B4:BF:E9:62:27:02".

## Issues & PRs to watch / +1

| # | Link | What it is | State |
|---|------|------------|-------|
| 1 | [habluetooth#602](https://github.com/Bluetooth-Devices/habluetooth/issues/602) | **Canonical feature request** — two mowers, two dedicated proxies, scoring makes them compete → reconnect cycles. Asks for optional MAC→scanner pin. | **open** (2026-08-01) |
| 2 | [habluetooth#628](https://github.com/Bluetooth-Devices/habluetooth/issues/628) | Bonded device locked onto proxy that cannot hold the bond → `error=5 Insufficient authentication` permanently. Proposes `BleakClient(source=…)` / `pinned=True`. | **open** (2026-09-05) |
| 3 | [habluetooth#604](https://github.com/Bluetooth-Devices/habluetooth/pull/604) | **Only code that exists** — branch `koan/pin-connection-source`, 2 commits, motivation is #602. Draft by `bluetoothbot`. | **open, draft** (2026-08-11) |
| 4 | [core#179667](https://github.com/home-assistant/core/issues/179667) | RSSI-based proxy flipping → asks for per-device proxy pinning (prior art). Assigned **bdraco**. | **open** (2026-08-20) |
| 5 | [core#172033](https://github.com/home-assistant/core/issues/172033) | Core-side twin of #602: two Automowers + two proxies → connect/disconnect cycle (`0x100`, `0x3e`). | **open** (2026-05-24) |
| 6 | [ha-ef-ble#445](https://github.com/rabits/ha-ef-ble/issues/445) | Downstream integration workaround: added "Preferred Bluetooth proxy" option in test build `v0.44.0-h.3`. Closed — solved locally, **not** upstream. | **closed** (2026-09-02) |
| 7 | [ha-ef-ble#451](https://github.com/rabits/ha-ef-ble/pull/451) | Implementation behind #445. States honestly: *"Nothing here can pin a device to a proxy"* — the integration layer cannot override habluetooth's routing. | **closed** (merged) |

## Community & docs

- [Community: Bluetooth adapter priorities](https://community.home-assistant.io/t/bluetooth-adapter-priorities/778863) — user asks for "prefer proxy, fallback to other adapters"; replies are "disable the others"
- [Community: Which Bluetooth Proxy is my device using?](https://community.home-assistant.io/t/which-bluetooth-proxy-is-my-device-using/720986) — info gap, debug logs only
- [Bluetooth integration docs — Multiple adapters](https://www.home-assistant.io/integrations/bluetooth/#multiple-adapters) — official stance: *"HA manages all Bluetooth adapters and scanners itself… Your integration never starts its own scanner"*

## See also

- [habluetooth issue tracker](https://github.com/Bluetooth-Devices/habluetooth/issues) — search for "pin" or "scanner"
- [Home Assistant Core Bluetooth issues](https://github.com/home-assistant/core/issues?q=label%3A%22integration%3A+bluetooth%22+pin)