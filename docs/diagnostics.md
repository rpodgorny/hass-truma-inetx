# Diagnostics

[← README](../README.md)

The device page's ⋮ → **Download diagnostics** dumps the config entry, whether
the last update succeeded, and the whole bus — every address the integration has
heard from, in hex, with everything that address published under it. That is the
evidence for what is on a given vehicle's bus, and the form addresses are read
and quoted in.

Each device also carries its own `param_meta`: what it says each parameter *is*,
as opposed to what it currently reads — its range, whether it can be written,
and for an enum the panel's own name for every value, with the ones this vehicle
cannot produce marked. Truma documents none of the protocol, but the panel
describes it in every frame, so a download answers "what does this value mean"
without anyone watching their heater and writing it down. The same descriptions
are logged, once each, at debug level.

`contested_topics` names the topics more than one device publishes on that
vehicle — usually none, which is why a single flat view of the bus looked
correct for so long. `unattributed` holds anything that arrived without a usable
source address; nothing reads it, and it should be empty.

A download can be read back with the
[dump tool](development.md#dumping-the-bus-without-home-assistant), which prints
the same bus device by device without Home Assistant in the way.

The BLE address, the panel's name and the persisted app identity (`muid` /
`uuid`) are redacted: the address is a private address that still pins the panel
to a location, and the identity is what the panel bonds against. The panel state
itself carries nothing identifying.
