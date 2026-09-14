"""Truma iNet X wire protocol — VENDORED THIRD-PARTY CODE.

`protocol.py` and `const.py` in this package come from
https://github.com/daaaaan/truma-inetx-ble (its `data/dbus-truma/service/`),
which carries **no licence** — copyright is retained by its author and no
redistribution rights are granted. They are kept here and quarantined in this
subpackage so the boundary is unambiguous: everything *outside* this directory
is original work by this repository's author and is GPL-3.0-licensed (see the
repository LICENSE).

`state.py` used to live here too. It no longer exists: the model it held —
one flat `Topic.Param` dict and a table of typed fields — asserted that every
topic has a single owner, which a bus does not (#9). What replaced it is
`../bus.py`, written here from the frames on the wire, so it is original work
and sits outside the quarantine.

Depending on upstream as a package would be preferable to vendoring, but its
`pyproject.toml` packages only `src*`/`tools*` while the protocol code lives
under `data/`, so there is nothing importable to depend on. If upstream ever
adds a licence and ships an installable package, delete this subpackage and add
the requirement to `manifest.json` instead — nothing else has to change.
"""
