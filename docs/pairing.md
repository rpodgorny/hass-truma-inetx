# Pairing

[← README](../README.md)

The panel uses **Just Works** pairing (no passkey) and bonds only while it is
actively in add-device mode. It is genuinely finicky — these rules matter, and
the first one is the counter-intuitive part:

1. Leave the panel on its **normal screen** — *not* in add-device mode. Its
   advertisement only carries a name in the scan response while it is idle, and
   discovery is keyed on that name, so an armed panel cannot be discovered on a
   first pairing.
2. In Home Assistant, get to the **"Pair with the Truma panel"** screen: either
   the discovered card, or Settings → Devices & Services → **+ Add Integration**
   → *Truma iNet X (BLE)*.
3. **Now** put the panel into add-device mode (Truma iNet X app, or on the panel
   itself) so its pairing screen is up.
4. Press **Submit once.** Repeated submits against a panel that is not cleanly
   ready make it show "something went wrong", and it then needs re-arming.

A virgin pairing usually completes in a few seconds. A *re-pair* over a panel
that still holds a bond for this Home Assistant can take longer than a minute —
that is the panel finishing a security exchange with a peer it already knows, not
a fault. If the panel is already configured, open the entry's **⋮** menu and
choose **Reconfigure** rather than adding it again; the **Configure** button on
that entry opens the poll-interval options, not the re-pair.

## If the panel never appears at all

The panel's advertisement carries one Truma service UUID and nothing else. Its
name rides the *scan response*, which only an **active** scan asks for. That
matters because discovery is keyed on the name — the address rotates every few
minutes, so keying on it would produce a new card per rotation — and a panel
Home Assistant has heard but cannot name is a panel it cannot offer.

**A panel in add-device mode advertises no name and no scan response at all**, so
it is exactly the panel that cannot be offered. That is why the pairing order puts
arming *after* the card is on screen. It does still put its Truma service UUID on
air, so Home Assistant *hears* an armed panel — it just cannot name it, which is
why the log shows `awaiting_name` rather than silence.

**If you armed it too early**, leave the add-device screen: the scan response
comes back, and the card appears within a minute. Then arm it again and press
**Submit once**.

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
holding it, BlueZ tries to encrypt with a key the panel no longer has and the
panel refuses the bond (`HCI 0x0E`) — retrying cannot help, because no pairing
exchange is ever attempted. The flow therefore drops this host's half and dials
again, and that drop is the one destructive act it will not take on its own: the
first attempt fails, the form comes back with **forget the stale bond**, and
ticking it is the consent. Nothing to do by hand, and nothing is dropped unless
the panel has already said no.

Measured on the van 2026-10-27: with a full-looking record the panel had
forgotten, twenty connects put **zero** pairing frames on air; after the drop the
same press bonded in nine.

That second case is why pairing no longer requires a connection to succeed
first. A panel with no bond drops every link it is offered, so on a host with
no proxy the connect and the bond were each waiting for the other: the BlueZ
pairing agent was never reached at all, and pairing ran out its timeout
re-dialling (#26). A connect that fails on every address the panel is
advertising now hands over to BlueZ, which bonds over its own connection.

## "Something went wrong during pairing" on the panel — check it, do not believe it

The panel may show **"Something went wrong during pairing, try again"** (wording
varies slightly) at the end of a pairing that in fact succeeded. It is reporting
**its own** pairing sequence, the one the Truma iNet X app would run. This
integration makes the Bluetooth bond itself and never runs that application-level
handshake, so the panel's sequence ends without a partner and the panel says so.

The bond is unaffected, and so is the session. Seen on a Panel 1 (twice, 2026-10-25)
and reported on a **Panel 2** (2026-10-26): the card appeared, pairing completed,
Home Assistant went on to publish every device behind the panel, and the panel
kept listing the host as a connected device while its screen said the pairing had
failed.

**Read these instead of the message:**

- On the panel, a **small green bar above the Bluetooth button** means it sees a
  central connected. That is the panel's own ground truth.
- In Home Assistant, the entry loads and its entities populate within a minute.

If both say the link is up, dismiss the message and change nothing. Re-arming and
submitting again on the strength of that message is how a working pairing gets
broken — repeated submits against a panel that is not cleanly in add-device mode
are exactly what makes it *need* re-arming.

If the entities stay empty or `unknown` instead, it was a real failure: work
through the list above.

## Where the bond lives

Pairing bonds the panel on whichever adapter or proxy Home Assistant connects
through at that moment, and the bond lives *there* — a BLE bond is per-adapter.
If that hardware later goes away, the panel has to be paired again. Where no
connection can be made at all, the bond goes on the local adapter that can see
the panel, because that is the only place a bond can be made without one.

To re-pair later, use **Reconfigure** on the device.

**Reconfigure declines while the bond is working.** If this Home Assistant is
talking to the panel — a session live now, or one that came up in the last five
minutes — the flow attempts nothing and tells you so, because unloading a
working session to re-bond over a bond that already works costs about half a
minute of data and changes nothing (measured on a real panel: one press, zero
SMP frames on air, zero refusals, bond untouched). A re-pair is for a panel that
has forgotten this host: remove this Home Assistant on the panel, clear its
saved device list, put the panel into add-device mode, then tick *forget the
stale bond* — that consent always runs the attempt, whatever the flow believes
about the bond.

**A failure says which case you are in.** If this host held no bond at all when
the attempt started, the error asks you to clear the panel's device list *first*
and then arm it, because that is the state a panel keeps an entry for a host
that no longer has its key — measured: a host with no record needed 11 connects
and about a minute to fail, and cleared only after the list was emptied and the
panel armed. A bond state that cannot be read is never reported as a missing
one: the flow only ever uses BlueZ's "not paired" as evidence, never "paired"
as proof.
