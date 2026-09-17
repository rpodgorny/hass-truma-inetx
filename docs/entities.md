# Entities

[← README](../README.md)

Every entity sits on the bus device that reported it, below the panel — see
[the bus model](bus.md).

| Entity | Platform | Notes |
|---|---|---|
| Truma iNet X | `climate` | Whichever modes the panel offers — Off / Heat / Fan-only everywhere, plus Auto, Cool or Dry where the vehicle has them. Offers only the control the current mode uses: the target temperature while heating or cooling, the fan speed as the fan mode (`off`, `1`–`10`) while venting. The setpoint follows the mode, because the panel keeps a separate field behind each one: heating writes the heater's, cooling writes the air conditioner's own, and each field's range is the one its owner describes (5–30 °C heating, 16–30 °C cooling) |
| Room temperature | `sensor` | °C |
| Water temperature | `sensor` | °C |
| Internal temperature | `sensor` | °C |
| Supply voltage | `sensor` | V |
| Water heating | `select` | Off / Eco (40 °C) / Comfort (60 °C) / Hot (70 °C) — the steps the panel offers |
| Electric heating | `select` | Supplemental electric element: off / 900 W / 1800 W — the steps the panel offers. Only where the vehicle has the element |
| Heating mode | `select` | `AirHeating.Mode`: Fast / Comfort — how hard the heater works on the air, the panel's own "fast" setting. Only where the heater reports it |
| Diesel burner | `switch` | Only where the heater has a diesel burner |
| Gas | `binary_sensor` | Whether the heater is drawing on gas. Read-only — the heater moves this itself. Only where it burns gas |
| Fan level | `number` | 0–10 |
| Flame | `binary_sensor` | Burner currently firing |
| BLE connection | `binary_sensor` | Diagnostic — is the panel connected |
| Fresh water | `sensor` | % — only where the vehicle has a tank sensor |
| Grey water | `sensor` | % — only where the vehicle has a tank sensor |
| Fresh water pump | `switch` | Only where the vehicle has one |
| Water boost | `switch` | `WaterHeating.BoostMode`. Only where the heater reports it |
| Faster water heating | `switch` | `WaterHeating.FasterHeatingMode`. Only where the heater reports it |
| Faster water heating time | `sensor` | Diagnostic, seconds — the duration beside it. Only where the heater reports it |
| Gas bottle level | `sensor` | % — one per Truma LevelControl sensor, on the sensor's own device |
| Gas bottle contents | `sensor` | kg — the same bottle by weight, `GasBtl.FillLevelW` |
| Gas bottle temperature | `sensor` | °C, as the bottle sensor measures it |
| Gas bottle remaining (raw) | `sensor` | Diagnostic, disabled by default — `GasBtl.RemTime`, unitless on purpose: the name says time, the panel shows neither, and nothing measured says what it counts |
| Sensor battery | `sensor` | Diagnostic, % — a Bluetooth sensor's own battery, on that sensor's device |
| Cooling temperature | `sensor` | °C — what the air conditioner measures. Only where the vehicle has one |
| Cooling level | `select` | `AirCooling.Mode`: Low / Mid / High / Max / Night / Auto — the unit's own names for the stages, and how hard it runs, on its own device |
| Cooling | `binary_sensor` | Whether the air conditioner is running, as opposed to standing by — `AirCooling.Active` 2 is the unit awake with its compressor off, measured on a FreshJet at 336 W against a 311 W base load (#23) |
| Cooling status | `sensor` | Diagnostic — off / running / idle, the same three states as the flame, with the wire value in the `raw` attribute |
| Refill mode | `switch` | The panel's "refill" button: while it is on the tank sensor measures continuously and the panel sounds a tone at full. Only where the vehicle has a tank sensor |
| Shore power | `binary_sensor` | Whether 230 V is connected. The panel publishes it (`System.Plugged`) and so does an electrical block (`LinePower.Plugged`); a vehicle with both gets one on each device, which is two sources rather than one reading |
| Fault | `binary_sensor` | Whether the appliance is reporting an error at all, on the appliance raising it |
| Fault code | `sensor` | Diagnostic — the code itself, to look up in the manual. An appliance can raise several at once, so the state is the one it listed first and the attributes carry `count` and the whole `errors` list, plus the `severity` and `resettable` of the one in the state. Unknown while there is no fault: 0 would be a code nobody can look up |
| Reset fault | `button` | Clears the fault, the way the panel's own reset does. Offered only while the appliance is raising something it calls resettable — a window left open above the heater is not |
| Timer *n* | `switch` | Arms or disarms one of the panel's six timer slots. Only the filled slots appear: a panel publishes all six whether or not it has six timers, and marks the empty ones unavailable. Filling one at the panel makes its pair appear within seconds |
| Timer *n* schedule | `sensor` | What arming it does — the state is the start time, and the attributes carry `timer_name` (the panel's own label for it, "23 °C" on the van), `start`, `end`, `weekdays` and `symbol`. Read-only: the panel describes the timer itself with `perm` 0, the way it describes its serial number, so timers are edited, added and deleted at the panel |
| Timers enabled | `sensor` | Diagnostic — how many slots are armed, in one reading |
| Panel clock | `sensor` | Diagnostic — the clock the timers fire off, as the panel keeps it. The integration sets it at every connect, so a timer an hour out has something to look at |
| Demo mode | `binary_sensor` | Diagnostic — whether the panel is staging its readings instead of measuring them. Read here, never written |
| Requesting energy | `binary_sensor` | Diagnostic — whether the appliance is asking for gas, diesel or electricity right now, on the appliance asking |
| Logic supply | `sensor` | Diagnostic, disabled by default — V, the panel's 5 V rail beside its 12 V supply |
| Time since last touched | `sensor` | Diagnostic, disabled by default — seconds since anybody used the panel. Off because it counts up on every update |
| Fault reset window | `sensor` | Diagnostic, disabled by default — seconds the appliance will keep accepting a reset |
| Power mode (raw) | `sensor` | Diagnostic, disabled by default — `PowerMgmt.PwrMode`, unlabelled: nothing measured says what its values mean |
| Temperature source (raw) | `sensor` | Diagnostic, disabled by default — which probe the panel takes the room temperature from |
| Climate state (raw) | `sensor` | Diagnostic, disabled by default — `RoomClimate.Active`, which reads 4 while heating, a value no appliance-level `Active` has been seen at |
| Display brightness | `number` | Config, % — the panel's daytime brightness |
| Night brightness | `number` | Config — the panel's dark-mode step, 1–10 on the panel measured |
| Display timeout | `number` | Config, seconds |
| Screensaver | `switch` | Config |
| Starter battery | `sensor` | V — only where something reports `VBat.Voltage` |
| Leisure battery | `sensor` | V — only where something reports `L1Bat.Voltage` |
| Flame status | `sensor` | Diagnostic — off / running / idle. Measured three times: a Combi 6 E against a shore-power meter (#15), a Combi 4 gas at the panel (#24), and a FreshJet roof unit on the same `AirCooling.Active` triple (#23): *idle* is the appliance on with the room already above its target, which the flame flag beside it cannot say. The number it was named from stays in the `raw` attribute, and a value nobody has named yet reads unknown rather than being folded into a state it does not belong in |
| Free slots | `sensor` | Diagnostic — how many bonds the panel has left, summed over the breakdown by device kind it publishes. On its own Bluetooth management device |
| Connection state | `sensor` | Diagnostic — the panel's own view of the link, `BleDeviceManagement.BleConnState` |
| State | `sensor` | Diagnostic — the raw `BleDeviceManagement.State` value |

The climate mode list and the three selects' options are not fixed: the panel
enumerates each parameter for the vehicle it is installed in. A van with no air
conditioner lists no cooling mode; a heater without the electric element does
not list 1800 W. The entities offer what the panel offers, falling back to the
full list where it describes nothing. The panel's own names for the values are
never shown — they arrive in the panel's display language, and the labels here
stay translatable.

Everything marked "only where" is created the first time the hardware behind it
reports a value, not up front. Vehicles differ far more than the protocol does —
a Combi D has no electric element and its panel never mentions the parameter, a
gas/electric Combi has no diesel burner, most vans have no tanks and no
electrical block — and an entity permanently unknown because the hardware does
not exist looks exactly like one unknown because the integration is broken.

The timers are the one "only where" driven by a flag rather than by the
parameter arriving. A panel has six fixed slots and always publishes all six —
each an `id`, a label, a start, an end and seven weekday flags — with `avail` 0
on the empty ones. Reading the flag rather than the values matters: an empty
slot is a full structure of zeros that would present as a timer at midnight
every day. The weekday flags are passed through as the panel's own seven: the
only timer measured repeats daily, which says nothing about which end of the
list is Monday, and a day guessed wrong would reach an automation without ever
looking wrong.

## What the panel offers and this does not

A panel publishes rather more than it shows. Four are left alone on purpose.

`System.FactoryReset` and `Install.InstallNow` are writable, and are a factory
reset with no undo and a firmware flash over a link that drops when the van is
driven. Neither is worth one tap in a dashboard.

`System.DemoMode` is writable too, and is read without being offered: in demo
mode the panel stages every reading, so seeing that it is on is worth an entity
and switching it on is not.

`Panel.Language` is writable and the panel publishes no list of what it accepts,
unlike every other enumerated parameter it describes. A guessed value would
change the language of the panel in the vehicle, so it waits for somebody to
change it at the panel and read back what the value became.

`System.Beep` is a write-only array of ten frequency-and-duration pairs, and
nothing measured says what the units are — so the "find the panel" button it
would make is a tone nobody has heard yet.
