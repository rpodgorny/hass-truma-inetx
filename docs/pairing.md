# Pairing

[← README](../README.md)

The panel uses **Just Works** pairing (no passkey) and bonds only while it is
actively in add-device mode. It is genuinely finicky — these rules matter:

1. Put the panel **freshly** into add-device mode (Truma iNet X app, or on the
   panel itself) so its pairing screen is up.
2. In Home Assistant the panel should appear as a discovered device. Otherwise
   Settings → Devices & Services → **+ Add Integration** → *Truma iNet X (BLE)*.
3. Press **Submit once.** Repeated submits against a panel that is not cleanly
   ready make it show "something went wrong", and it then needs re-arming.

Pairing normally completes in a few seconds.

## If the panel never appears at all

The panel's advertisement carries one Truma service UUID and nothing else. Its
name rides the *scan response*, which only an **active** scan asks for. That
matters because discovery is keyed on the name — the address rotates every few
minutes, so keying on it would produce a new card per rotation — and a panel
Home Assistant has heard but cannot name is a panel it cannot offer.

Home Assistant's default scanning mode, `auto`, starts passive and turns the
radio active only for scheduled windows: four minutes after the scanner starts,
then once every twelve hours. So both entry points — the discovery card and
**+ Add Integration** — ask for a short active window of their own when they
have nothing to offer, rather than waiting one out.

An adapter pinned to `passive` never opens one, and the panel is never offered.
Pair on `auto` or `active`.

A host that has bonded this panel before hides all of this: BlueZ keeps the
name and hands it over whatever the scan mode. The cache goes when the bond
does, which is exactly the state a first pairing is in.

## If pairing fails

Work through these in order, always re-entering add-device mode before each
attempt, since the panel only accepts a bond while its pairing screen is up:

1. **Clear the panel's saved Bluetooth device list.** It stores only ~4 devices
   and silently rejects new bonds once full. Clear it in the Truma iNet X app,
   re-arm add-device mode, try again.
2. **If that did not help, power-cycle the panel and start over.** Off and on,
   back into add-device mode, repeat the whole pairing step. This drops "ghost"
   connections holding one of the panel's connection slots, and makes it
   advertise a fresh Bluetooth address that pairs cleanly. Resolves most
   stubborn cases.

You do **not** need to clear any bonds, on either side of the link. If a
**proxy** still holds a bond the panel has forgotten, the panel rejects it on
that one address only (`error: 97`), and the integration rotates to the panel's
next address, which pairs normally. If **this host's own adapter** is the one
holding it, BlueZ offers a key the panel no longer has and the panel drops the
link before its services resolve — so pairing asks the panel first and, only
once the panel has refused, drops the host's bond and pairs again. Nothing to
do by hand, and nothing is dropped unless the panel has already said no.

That second case is why pairing no longer requires a connection to succeed
first. A panel with no bond drops every link it is offered, so on a host with
no proxy the connect and the bond were each waiting for the other: the BlueZ
pairing agent was never reached at all, and pairing ran out its timeout
re-dialling (#26). A connect that fails on every address the panel is
advertising now hands over to BlueZ, which bonds over its own connection.

## Where the bond lives

Pairing bonds the panel on whichever adapter or proxy Home Assistant connects
through at that moment, and the bond lives *there* — a BLE bond is per-adapter.
If that hardware later goes away, the panel has to be paired again. Where no
connection can be made at all, the bond goes on the local adapter that can see
the panel, because that is the only place a bond can be made without one.

To re-pair later, use **Reconfigure** on the device.
