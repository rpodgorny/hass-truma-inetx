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

You do **not** need to clear any bonds on the Bluetooth proxy. If the proxy
still holds a bond the panel has forgotten, the panel rejects it on that one
address only (`error: 97`), and the integration rotates to the panel's next
address, which pairs normally.

## Where the bond lives

Pairing bonds the panel on whichever adapter or proxy Home Assistant connects
through at that moment, and the bond lives *there* — a BLE bond is per-adapter.
If that hardware later goes away, the panel has to be paired again.

To re-pair later, use **Reconfigure** on the device.
