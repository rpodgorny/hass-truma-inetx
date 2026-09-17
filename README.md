# Truma iNet X (BLE) — Home Assistant integration

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml/badge.svg)](https://github.com/rpodgorny/hass-truma-inetx/actions/workflows/validate.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Local push integration for the **Truma iNet X** control panel over Bluetooth LE.
Heating, water heating, electric level, diesel burner, fan, timers, tanks, gas
bottles, air conditioning and faults — whatever the vehicle's bus reports. No
cloud, no Truma account, no LIN wiring.

Developed against an iNet X driving a **Truma Combi**. Other Truma appliances
speak the same protocol but are untested; reports welcome.

> ### ⚠️ Updating from 0.8.x: **every entity id changes**
>
> The 0.9.0 betas moved each entity onto the bus device that reports it, so the
> unique id now carries a device address, and **there is no migration**: old
> entities linger as `unavailable`, history does not carry over, automations and
> dashboards need repointing, and hand-enabled entities come back disabled.
>
> Tidiest route is to **delete the config entry and set it up again from
> scratch** — not necessary, but it trades the cleanup for one re-pairing.
> Details and the rest of the fallout: [upgrading](docs/upgrading.md).

## What you need

Either a **local Bluetooth adapter** on a host whose kernel resolves the
panel's rotating address (Linux below 6.19), or an **ESPHome Bluetooth proxy**
on stock `esp-idf` firmware, which works on any host. Neither is a fallback for
the other — which you have decides. On kernel 6.19+ a local adapter usually
does not work: [reaching the panel](docs/connectivity.md) has the kernel
commits, the proxy config and how Home Assistant picks the path.

## Install

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rpodgorny&repository=hass-truma-inetx&category=integration)

Or by hand: HACS → ⋮ → **Custom repositories** → add
`https://github.com/rpodgorny/hass-truma-inetx` as category **Integration**,
install **Truma iNet X (BLE)**, restart Home Assistant. Without HACS, copy
`custom_components/truma_inetx/` into `config/custom_components/` and restart.

## Pair

The panel uses **Just Works** pairing and bonds only while its add-device
screen is up:

1. Put the panel **freshly** into add-device mode (Truma iNet X app, or at the
   panel).
2. Settings → Devices & Services: the panel should be discovered. Otherwise
   **+ Add Integration** → *Truma iNet X (BLE)*.
3. Press **Submit once**. Repeated submits make the panel need re-arming.

Usually done in seconds. If not — the panel stores only ~4 bonds and rejects
new ones when full: [pairing](docs/pairing.md).

## What you get

The panel is a gateway, not a heater remote, so Home Assistant gets the bus:
the panel is a hub device and every appliance that has published something —
heater, air conditioner, electrical block, gas-bottle sensor — appears below it
with its own entities. Two of a kind therefore work: two bottles are two
sensors, and cooling reaches the roof unit rather than the heater.

One `climate` entity drives heating, cooling and venting, with the control the
current mode actually uses. Around it: temperatures, tank levels, gas bottles,
batteries, shore power, faults with a reset button, the panel's six timer
slots, display settings, and a set of diagnostic sensors. Entities appear only
once the hardware behind them reports, so a vehicle without a tank gets no tank
sensor.

Full table: [entities](docs/entities.md). Why the devices are shaped this way,
and what has been measured on which vehicle: [the bus model](docs/bus.md).

A **thermostat card** ships with the integration — dial sets temperature while
heating, fan speed while venting. Nothing to install: edit a dashboard, **+ Add
card**, search *Truma*. Details: [dashboard card](docs/card.md).

## When something is wrong

⋮ → **Download diagnostics** on the device page dumps the whole bus, every
address with everything it published and what the panel says each parameter
*is*. That is the evidence any issue report needs:
[diagnostics](docs/diagnostics.md). It can be read back offline with
`tools/dump_bus.py`.

Reconnects can still wedge for minutes and clear by themselves; that and the
rest are in [known limitations](docs/limitations.md).

## Docs

- [Upgrading from 0.8.x](docs/upgrading.md) — the entity rename, and what to do
- [Reaching the panel](docs/connectivity.md) — RPA, kernels, proxies, addresses
- [Pairing](docs/pairing.md) — the finicky bits, and where a bond lives
- [Entities](docs/entities.md) — every entity, and what the panel withholds
- [The bus model](docs/bus.md) — devices, naming, measurements, read-only calls
- [Dashboard card](docs/card.md)
- [Diagnostics](docs/diagnostics.md)
- [Known limitations](docs/limitations.md)
- [Development](docs/development.md) — bench dumps without HA, tests
- [Credits and licensing](docs/licensing.md)

## Licence

GPL-3.0 ([LICENSE](LICENSE)), except the vendored protocol code in
`custom_components/truma_inetx/truma/` and the Truma artwork — see
[credits and licensing](docs/licensing.md). "Truma" and the Truma iNet X mark
are trademarks of Truma Gerätetechnik GmbH & Co. KG; this project is not
affiliated with or endorsed by Truma.
