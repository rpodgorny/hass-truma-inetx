# Real diagnostics downloads

[← README](../README.md)

Every fact in [the bus model](../docs/bus.md) came out of a download like these,
and until now each one lived in a temporary directory until it was lost. They
are kept here instead: they are the only evidence for what is on a vehicle's
bus, they are what a parser regression would be caught by, and they cost tens of
kilobytes.

Read one without Home Assistant:

```bash
./tools/dump_bus.py dumps/combi4-inetx-pro/water-boost.json
```

Add one:

```bash
./tools/import_dump.py ~/Downloads/config_entry-truma_inetx-*.json \
    --vehicle combi4-inetx-pro --state water-boost --issue 22
```

`import_dump.py` scrubs and files it, and prints the index row to paste below.
It **refuses to write** a file that still has an address-shaped string in it
anywhere — which is not the same as trusting the integration's own redaction:
that one matches key names, and a download taken before `discovery_keys` joined
`TO_REDACT` hides the panel's address inside a `repr` string no key name
reaches. All three files below were like that.

Scrubbed: the BLE address, the panel's name, the entry title and unique id, the
discovery keys. **Kept on purpose:** serial numbers, `Identify.UniqueID`, the
certificate thumbprint and every label the panel publishes — a timer's own name
is evidence, and a serial is how two devices of one class are told apart after a
re-pairing. Anything here was already posted publicly on the issue it came from.

## combi4-inetx-pro

An **iNet X Pro** panel (`0x0101`, 61 parameters, HW 3.4, SW 3.5) driving a
**Combi 4** (`0x0201`, 33), with an electrical block at `0x0405` (31) and the
panel's own BLE device management at `0x0601` (10). The block is the vehicle's
whole 12 V side: fresh and grey water levels with their measure requests,
`VBat`/`L1Bat` voltages, `LinePower.Plugged`, the fresh-water pump switch and a
floor-heating flag. From
[#22](https://github.com/rpodgorny/hass-truma-inetx/issues/22), Home Assistant
2026.8.3.

| State | Shape | Addresses | Written by | What it settles |
|---|---|---|---|---|
| `normal-heating` | flat | — | 0.7.0 | `AirHeating.Mode` 1 while heating at `RoomClimate.Mode` 3 — the pair to the next one |
| `fast-heating-mode` | flat | — | 0.7.0 | the same vehicle with the panel's "fast" setting on: `AirHeating.Mode` 0 and nothing else moved, which is what made it the heating-mode select rather than water priority |
| `water-boost` | per-device | `0x0101`, `0x0201`, `0x0405`, `0x0601` | 0.8.0 | `WaterHeating.FasterHeatingMode` 1 with the room off; the only dump here carrying `param_meta`, so it is where the panel's own ranges come from (brightness 10–100, night step 1–10, display timeout to 4294967295) and where the six timer slots, their `avail` flags and their `DefaultTimer` labels are readable |

The two 0.7.0 files are the flat shape: one `raw_params` dict of 107 values with
a `topic_source` beside it, no per-device store, so `dump_bus.py` lists them
under the bus rather than under a device. That is deliberate — a value whose
publisher was never recorded cannot be assigned to one now without guessing.

## Wanted

- A download from **this** vehicle on 0.9.x, for a bus-shaped file to sit beside
  the flat ones.
- Anything with **two of a kind** — two gas bottles, a roof air conditioner
  beside a Combi — which is the one shape no dump here has.
- The nRF Connect captures from
  [#6](https://github.com/rpodgorny/hass-truma-inetx/issues/6): the Panel 2
  advertisement and GATT table, which is what 0.8.2b1's UUID fix was read off
  and which exists only as screenshots on that issue.

Raw BLE captures (btsnoop, pcap) stay out of this repository: they are large and
they carry link-layer addresses that no scrubber here can see inside. Keep them
out of tree and distil what they show into [the docs](../docs/limitations.md).
