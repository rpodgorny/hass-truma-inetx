# Development

[← README](../README.md)

## Dumping the bus without Home Assistant

`custom_components/truma_inetx/bus.py` is the protocol's own model of the
panel's bus and imports no Home Assistant, so it can be run on its own — the
only way to debug the protocol on the bench. `tools/dump_bus.py` is the
launcher; it prints every device on the bus, everything each one publishes, and
what each device says those parameters are.

```bash
./tools/dump_bus.py diagnostics.json        # read somebody's download back
./tools/dump_bus.py --live --identity ~/homeassistant/.storage/truma_inetx_<entry id>
```

Reading a download needs nothing but the standard library. `--live` needs
`bleak`, and the app identity the panel is bonded to — a panel only talks to one
it has been paired with, and Home Assistant stores that under
`.storage/truma_inetx_<config entry id>`. Add `--name 'Truma iNetX-XXXXXX'` if
more than one panel is in range, `--json` for machine-readable output, `--debug`
to log every frame.

```
0x0201  class 0x02 instance 1  Combi 6 E  serial 12345678  (31 parameters)
    AirCirculation.FanLevel                      4   [0..10; perm 1]
    AirHeating.Temp                              228
    ...

Topics with more than one publisher -- no flat reading of
these can mean anything:
    AirCirculation           0x0201, 0x0406
```

Real downloads are kept in [`dumps/`](../dumps/README.md) — the evidence for
vehicles nobody here can plug into, read by the tool above and by the tests.
File a new one with `tools/import_dump.py`, which scrubs it and refuses to
write anything with an address-shaped string left in it.

## Tests

The checks in `tests/` are self-contained: they stub Home Assistant, bleak and
dbus, so they need neither an HA install nor hardware, and each file is a script
— run one directly, or run the lot:

```bash
python3 tests/test_entry_teardown.py              # unload leaves nothing running
for t in tests/test_*.py; do python3 "$t" || break; done
```

Most need nothing installed. Seven reach code that imports a library and the
loop above stops on them unless it is there — `voluptuous` for the config flow's
schema (`test_panel2_discovery.py`), `cbor2` for the six that reach the protocol
module, whether to build real frames and parse them back or by way of the
coordinator that imports it:

```bash
pip install voluptuous cbor2==5.6.5
```

`tests/stubs.py` holds the shared Home Assistant stand-ins; it is not a test and
runs nothing on its own.

CI runs every file too, in two stages: everything that needs no library on a
bare interpreter first, so a test double that quietly grows an `import cbor2`
fails there instead of passing because a later step had already installed it.
Adding a test file is enough to have it run — there is no list to keep in step,
which is how five of them went uncovered for the whole 0.9.0 beta series.
