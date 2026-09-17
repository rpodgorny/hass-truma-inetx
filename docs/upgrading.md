# Upgrading from 0.8.x: every entity id changes

[← README](../README.md)

The 0.9.0 betas put each entity on the bus device that reports it, so its unique
id now carries a device address. **There is no migration.** These consequences
were measured on the upgrade, not predicted:

- Old entities stay in the registry as `unavailable` until you delete them from
  the device page. History does not carry over.
- Automations, scripts and dashboards that name an entity have to be pointed at
  the new one.
- An entity that ships disabled and that you enabled by hand comes back
  disabled, and the choice cannot be recovered from the old entry. The three
  that ship disabled: internal temperature, supply voltage, raw flame status.
- Where an old entity still holds the name a new one wants, the new one keeps a
  `_2` suffix — even after the old one is deleted.

Tidiest route: **delete the config entry and set the integration up again from
scratch** (panel into add-device mode, pair once). No stale entities, no `_2`
names, nothing to clean up by hand. It is **not necessary** — updating in place
works — it trades the cleanup for one re-pairing.

No migration is possible: a unique id that meant "this parameter, somewhere on
this panel" cannot be mapped onto one that means "this parameter, on this
device" without guessing which device. And the integration is still in
development. Why the shape changed: [the bus model](bus.md).

A vehicle that had the electric select or the diesel switch before they became
conditional is the same case. The integration never removes entities itself: a
parameter not reported *yet* is not hardware that does not exist.

Earlier break, 0.7.1b4: the water select's options went from `40 °C / 60 °C /
70 °C` to `Eco (40 °C) / Comfort (60 °C) / Hot (70 °C)`. A script calling
`select.select_option` with an old string has to be updated; the values on the
wire are unchanged.
