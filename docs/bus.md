# One device per device

[← README](../README.md)

The panel is not a heater remote. It is a gateway onto a TIN bus of Truma
appliances, a CI bus of vehicle electrics and third-party air conditioners, a
CAN bus and Bluetooth gas sensors. Home Assistant is given that shape: the panel
is the hub, every bus device that has published something appears below it, and
its entities are built from what *it* reported.

That is what makes two of a kind possible. A Combi and a Dometic roof air
conditioner both publish `AirCirculation.FanLevel`; two Truma LevelControl
bottle sensors publish the whole of `GasBtl` — so each gets its own fan and its
own level instead of overwriting the other (#9). Writes follow the same rule:
they go to the device the entity belongs to, which is why cooling reaches the
roof unit that does it rather than the heater that silently dropped it (#10).

## Naming and re-pairing

Devices are named from what the bus says: the panel's name for them
(`Identify.Name`), plus the owner's own label where there is one — a gas-bottle
sensor publishes `GasBtl.Name`, the word you typed at the panel, so two bottles
are "Truma LevelControl Links" and "… Rechts" rather than two of one name. With
no label, or two devices sharing one, the address instance separates them
("Truma LevelControl 3", "… 4") — but only where there is something to
separate: a bus whose names already differ keeps them whole. A device
publishing no name at all is named after its address, because a device class
is not a product: a Dometic air conditioner and a Schaudt electrical block
share one.

An entity waits for its device to be named before it is created. Home
Assistant builds an entity id from the device's name and never revises it, and
the name arrives late — subscribing makes the panel push values seconds before
the descriptions that carry `Identify.Name` are asked for — so an entity built
on the value alone would carry `bus_device_0x0201` for good. The wait costs
seconds and ends when discovery does, so a device that names itself nothing
still gets its entities, under its address.

Bus addresses are reassigned when a device is re-paired, so a re-pairing means
new Home Assistant devices and new entities. The label survives it — it is
stored in the device and comes back with it — the address does not, so two
identical bottles that swap addresses keep their own names. If your devices
publish no label, take a [diagnostics download](diagnostics.md) *before*
re-pairing: it lists each address with the serial number that answered on it,
and the serial is on each device page afterwards.

Entity ids changed with this shape in the 0.9.0 betas — see
[upgrading](upgrading.md).

## Why some parameters are read-only

**Gas is a sensor, not a switch.** `EnergySrc.GasLevel` is writable and the
write does go through, but the heater writes it too: on a gas/electric Combi,
switching the electric element off was measured turning the gas source on by
itself. A control over something the appliance also drives would fight it and
flap, so the reading reflects the heater's choice rather than pretending to make
it.

**The three-state `Active` family.** `System.FlameStatus`, `AirCooling.Active`
and the other parameters the panel gives type 105 take 0, 1 and 2 — off,
running, and the appliance on but standing by. Each gets a flag that is on for
1 alone, never "anything non-zero": a burner idling with the room already warm,
or a roof unit awake with its compressor off, is not the vehicle being heated
or cooled, and the flag is what an automation acts on. Beside each flag sits a
diagnostic sensor naming the three states, with the wire value in a `raw`
attribute, so a fourth value reads unknown rather than being folded into a
state it does not belong in.

Measured three times, on different hardware: a Combi 6 E against an
independent shore-power meter (#15), 1 → 2 in the second the draw fell from
1787 W to 105 W; a Combi 4 gas at the panel (#24), which put the idle state in
words; and a Dometic FreshJet roof unit (#23) reporting 2 with cooling
selected, drawing 336 W against a 311 W base load and 585 W while actually
running.

## The two water-priority switches

They are the panel's way of putting the burner's whole output into the boiler.
The reverse-engineered schema lists `WaterHeating.BoostMode` and
`WaterHeating.FasterHeatingMode` as separate 0/1 parameters, the second with a
duration in seconds beside it. Which one the panel's own button writes was the
open question in #7, and on the two vehicles read so far it cannot be
`BoostMode`: a gas Combi (#22) and a diesel van both carry `FasterHeatingMode`
and `FasterHeatingModeTime`, neither has a `BoostMode` at all, and the panel
that offers a boost sits on one of them. So the switch labelled "Faster water
heating" is the panel's boost. `BoostMode` stays in the code because the schema
lists it and some other vehicle may yet have it; both are offered, each waits
for its own parameter, so a heater reporting neither is given neither.

The *write* is pinned. On the diesel van the switch wrote
`WaterHeating.FasterHeatingMode = 1` to the heater at 0x0201, the panel
republished the parameter with its `avail` flag moved from 0 to 1, and
`FasterHeatingModeTime` counted down from its declared maximum of 2400 at one
per second — so the duration beside it is time remaining, not the length the
mode was configured for. That flag says whether a value is in effect *right
now*, not whether the appliance has the feature, which is why an entity is still
created for a parameter the panel currently marks unavailable.

The write is also conditional. Sent while the panel was venting it was
acknowledged and not applied, and the panel put it into effect 226 ms after room
climate left `Ventilating`, without being asked again. An acknowledgement from
the transport says the panel took the frame, not that it acted on it — the shape
of #10, and no offline test can tell the two apart.

The "Heating mode" select is the same kind of finding, gone the other way. The
schema listed `AirHeating.Mode` as `Fast=0, Comfort=1` with no vehicle behind
it; the two dumps in #22 toggle the panel's "fast" setting and move that
parameter and nothing else — water heating was off in both, so this is the air
heating's own mode rather than the water taking priority. A second panel offers
the same two choices under the same names, and reads back `Comfort` on a running
van.

The water select's labels (`Eco (40 °C) / Comfort (60 °C) / Hot (70 °C)`) name
what the panel writes and say what the name means. A second vehicle's owner
reads the three as stages with no temperature behind them and holds that the
numbers do not match a Combi 6 E; the labels stay until that is measured rather
than read off a screen.

## What has actually been measured, and where

**On a vehicle as of 0.9.0b2**: a diesel Combi D 4 GEN2 behind an iNet X Pro.
The session comes up, parameter discovery reaches all 18 seeded addresses and
every one acknowledges, and the bus resolves to three publishers — the panel at
0x0101 with 67 parameters, the Combi at 0x0201 with 33, the panel's own BLE
device management at 0x0601 with 10. The heater appears below the panel as its
own device, named and serialled from what it reported. Writes land on the device
that publishes the parameter: setting a fan speed sent `RoomClimate.Mode` to
0x0101 and `AirCirculation.FanLevel` to 0x0201 in the one gesture, and the panel
echoed both back. Four topics on that bus have more than one publisher
(`Identify`, `ErrorReset`, `PowerMgmt`, `DeviceManagement`), all metadata; a
vehicle carrying two appliances that publish the same *reading* is still unread.
[#23](https://github.com/rpodgorny/hass-truma-inetx/issues/23) has what is left.

**Not run here**: the air conditioner, the gas bottle's weight and temperature,
the sensor battery and the refill mode come from a second vehicle — a Weinsberg
with a Combi 6 E, a Dometic FreshJet 2200 at 0x0406, an electrical block and two
Truma LevelControl bottles — measured between 2026-09-02 and 2026-09-04 in
[MarioDeMonti's fork](https://github.com/MarioDeMonti/hass-truma-inetx), each
value read off the panel beside the parameter. Two things follow. The cooling
setpoint is the air conditioner's own `AirCooling.TgtTemp` and not the heater's
field, which is measured; the setpoint automatic uses is `RoomClimate.TgtTemp`,
which is *not* — it is the field left over, and the panel decides in automatic.
And `GasBtl.RemTime` is carried without a unit on purpose: the name says time,
the panel shows a percentage and a weight, and on that vehicle both bottles read
250 while one was 51 % full and the other 100 %, which is not a remaining
anything.

**The fault code and the reset button** are measured here, on the van, by
opening a window above the heater: it raises
`[{"sev": 1, "code": 412, "resettable": 0}]` within seconds, on the heater
rather than on the panel, whose own error list stays empty. That is also the
reset button's rule — the appliance says per fault whether it can be cleared,
and offering a button it has already said will do nothing is worse than offering
none.

**The panel's own entities** — shore power, the fault flag, the timers and the
four display controls — come from a third vehicle, a Combi 4 gas behind an iNet
X Pro, from the parameter dumps in
[#22](https://github.com/rpodgorny/hass-truma-inetx/issues/22), which are kept
in [`dumps/combi4-inetx-pro/`](../dumps/README.md). Their ranges come from there
too: the panel describes its own brightness as 10–100 and its night step as
1–10. Its display timeout it describes as 0 to 4294967295 seconds, which is the
width of the field rather than a control, and is the one range this integration
clips.

That vehicle is also where the 12 V side was read, which nothing here said
before the dumps were kept: an electrical block at 0x0405 publishes
`FreshWater.Level` and `GreyWater.Level` with their measure requests,
`VBat.Voltage` and `L1Bat.Voltage`, `LinePower.Plugged` and
`Switches.FreshWaterPump` — so the tank levels, the two batteries and the pump
switch are measured there rather than inferred from the schema, and it is the
vehicle that shows shore power arriving from two publishers at once (the panel's
`System.Plugged` beside the block's `LinePower.Plugged`).

## Push, and the one thing that is polled

Updates are pushed as the panel sends them (roughly 25 frames/minute). The tank
levels are the exception: a tank sensor answers with the level it measured when
it was last *asked*, and nothing on the bus asks it except the panel, when its
water screen is opened — so a tank emptied by hand would keep reporting its old
level indefinitely. The integration asks for a fresh measurement once per
connect and every 60 s while the link is held, addressed to whichever device
reported the tank.

## One control at a time

The panel drives its own fan while heating and has no setpoint at all while
venting, so exactly one of the two controls is meaningful at any time. The
climate entity reflects that: `supported_features` follows the mode instead of
advertising both. Off keeps the setpoint, the way every other Home Assistant
thermostat does — it is the resting target you come back to. The `number` entity
exposes the fan level in every mode, for automations.
