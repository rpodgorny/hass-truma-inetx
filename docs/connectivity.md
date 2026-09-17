# Reaching the panel

[← README](../README.md)

The panel advertises a **fast-rotating Resolvable Private Address** and accepts
an encrypted reconnect only from a client that puts that current address on air.
Two kinds of hardware manage that, either is enough:

- a local Bluetooth adapter on a host whose kernel or controller resolves the
  address;
- an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html),
  whose ESP-IDF controller resolves it itself and therefore works on any host.

A local adapter is not a fallback and a proxy is not a requirement. Which one
you have decides. On Linux the kernel version decides whether the first is
available at all:

- **Below 6.19** a local adapter reconnects fine. `hci_connect_le()` substitutes
  the peer's cached RPA for the identity address before putting a connection on
  air, so no LL Privacy and no proxy is needed. Kernel 6.12 — the Pi 5 in
  [#13](https://github.com/rpodgorny/hass-truma-inetx/issues/13) — is in this
  range, and `14b06c3a88f7` has not been backported to any 6.12, 6.17 or 6.18
  stable release.
- **6.19 and later** it usually does not. Commit
  [`14b06c3a88f7`](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=14b06c3a88f7)
  keeps the identity address all the way to the controller, so the panel never
  hears the connect. A local adapter then works only if the controller supports
  LL Privacy *and* BlueZ has programmed the panel's IRK into its resolving list,
  which currently does not happen for dual-mode bonds
  ([bluez#2356](https://github.com/bluez/bluez/issues/2356)).

A kernel fix is
[posted to linux-bluetooth](https://lore.kernel.org/linux-bluetooth/20260908012048.3681904-2-radek@podgorny.cz/)
and working its way upstream. Until it lands, a proxy is what works on 6.19+.

Older reports in this repo claim BlueZ can never do this. Wrong: the adapters
tested happened to run kernels carrying the regression.

## Who picks the path

**Not the integration.** Home Assistant picks, and re-picks at every connect,
scoring each path by signal, by how often connects to that address have already
failed on that adapter, by connects in flight and by free connection slots.
`logger: logs: habluetooth: debug` shows the paths found and the ranking.

The integration decides which *address* to dial, learned per host: the first
session that works records whether this panel answers on a rotating address or
on its identity address, and later connects start there. A host whose answer
changes — kernel upgrade, moved proxy — falls back to the other kind by itself,
costing one attempt rather than the connection.

The learned answer is in the [diagnostics download](diagnostics.md) as
`address_kind`, beside `session_transport` — `proxy` or `local`, the adapter the
last session that came up actually ran over. Nothing dials on that; it is there
because a host can bond over one adapter and then run every session over the
other, and a download naming only the address cannot say so.

## Running a proxy

Stock firmware is enough; nothing custom is needed.

```yaml
esp32:
  framework:
    type: esp-idf   # required: more connection slots + in-controller RPA resolution

bluetooth_proxy:
  active: true
```

Put the proxy **within a few metres of the panel**. Distance shows up as
`ESP_GATT_CONN_FAIL_ESTABLISH` connect failures rather than a clean error.

If the integration hears the panel advertising but cannot connect to it
repeatedly, it raises an issue under Settings → **Repairs** saying so, rather
than leaving the entities unavailable with no explanation. It stays quiet while
the panel is switched off or out of range — not the same fault — and clears the
issue on the next successful connect.
