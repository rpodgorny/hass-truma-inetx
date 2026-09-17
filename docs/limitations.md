# Known limitations

[← README](../README.md)

## Reconnects can wedge

If the link drops, reconnecting sometimes fails repeatedly. A btsnoop of one
recovery on the diesel van says what it is: 53 `LE Create Connection` commands
inside a minute, every one to the same resolvable private address, every one
answered `LE Connection Complete: Success` — and 52 of them dropped 250–500 ms
later with `Connection Failed to be Established` (0x3e), before encryption was
ever started. The panel takes the link and lets go of it again until it is ready
for a session.

Address rotation cannot help: on a host whose kernel resolves the panel's RPA
the address the integration picks is the identity one and the kernel substitutes
its own cached RPA underneath, so `avoid` only ever demotes an address that
never goes on air. It clears by itself — longest seen 15 minutes, no power-cycle
needed.

A second failure used to wear the same face, one layer up, and is **fixed**. The
link would come up, the panel would send a frame or two and then answer nothing
while still announcing an incoming message every five seconds, and the session
would hang inside its first write: BlueZ's Write Request has no timeout of its
own, and the stall watchdog does not start until startup has finished. Measured
on the van on 2026-09-15 it sat there for four minutes, "connected" and carrying
nothing, until the link was forced down from outside — and would have sat there
for as long as Home Assistant ran. Writes, the disconnect and startup as a whole
are each bounded now, so that link is given up after five seconds and retried.

## Reloading the config entry used to leave it unloaded — fixed

Anything that reloaded the entry while a session was live tore the integration
down without bringing it back, and enabling or disabling one of its entities is
enough, because Home Assistant reloads on that by itself. The BLE client was
orphaned still connected, feeding a coordinator that had stopped and holding one
of the panel's connection slots, so the entry could not be reloaded by hand
either; only a restart cleared it. Measured on the van, 2026-09-15.

The teardown was in the wrong order. Home Assistant cancels a config entry's
background tasks *after* `async_unload_entry` returns, so the session task
outlived the unload and was killed by that cancellation — and a task cancelled
mid-flight cannot run its own teardown, because the first `await` in its
`finally` raises straight away. The stop now ends the session task and waits for
it (five seconds, then cancels it and says so in the log) before closing
anything, so the link is given up by a session that is still alive. Two
neighbours of the same shape went with it: the live connection a pairing hands
to setup is now closed if no session ever adopted it, and a setup that fails
after the session has launched stops it on the way out rather than leaving it
running behind an entry Home Assistant will never unload.

Measured fixed on the van, 2026-09-17, on 0.9.0b14, by the trigger that found
it — toggling one entity. The session closed its own link (`BLE link closed
cleanly`), the stop's own disconnect found nothing left to do, and the entry came
back by itself: transport up ten seconds later, startup finished at thirty-four,
parameter discovery and all, no restart and no `did not stop within` warning.
Under 0.9.0b13 the line after that first one was `no live BLE link to close`,
and nothing followed it.

One case cannot be closed from here: a task cancelled while
`establish_connection` is still inside itself never returns the link it was
opening, so nothing can close it and the panel drops it in its own time. The log
names that case. The two neighbours — the un-adopted pairing link and a setup
that fails after its session launched — are pinned by
`tests/test_entry_teardown.py` but have not been triggered on hardware, since
reaching them means re-pairing or forcing a setup failure.

## Smaller ones

- **Duplicate entries in the panel's device list.** Each pairing can leave an
  extra record. Harmless so far, but it consumes the panel's ~4 slots.
- **A tank sensor that stops answering stops being refreshed.** A tank reports
  the level it last measured and nothing else, so it is asked once a minute
  (issue #4). If the panel withholds the acknowledgement — which it does for a
  frame addressed to a device that is no longer there, after a re-pairing or a
  removal — the sensor is asked three times and then left alone until it
  publishes something of its own, with one warning in the log naming the
  address. The entity keeps its last reading, which is honest but will not
  move. Asking regardless was worse: an unanswered request of that kind ends
  the session, so it cost a reconnect a minute.
- Only the local name / service UUID are used for discovery; the stored address
  is treated as volatile because it rotates.
