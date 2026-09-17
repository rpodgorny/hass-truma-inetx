"""What a session with the panel consists of, with no Home Assistant in it.

Registration, subscription, identity and parameter discovery: the sequence
that turns a connected GATT link into a bus you can read. It lives here rather
than in the coordinator so that it can be run from a terminal against real
hardware -- see ``bus._main()`` -- which is the only way to debug the protocol
without a Home Assistant install in the way.

Everything here takes the transport as an argument. ``client`` is anything with
``assigned_addr``, ``connected`` and an awaitable ``send(frame, probe=False)``;
``ble.TrumaBleClient`` is the real one and the tests pass a recorder.
"""

from __future__ import annotations

import asyncio

from .bus import Bus, MeasureMiss
from .const import LOGGER
from .truma.const import (
    CTRL_MBP,
    DEV_APP_DEFAULT,
    DEV_BROADCAST,
    DEV_MSG_BROKER,
    DEVICE_SEED,
    MBP_PARAM_DISC,
    MEASURE_REQUEST_PARAM,
    MEASURE_REQUEST_TOPICS,
    TOPIC_BATCHES,
)
from .truma.protocol import (
    build_identity_frames,
    build_register_frame,
    build_subscribe_frame,
    build_v3_frame,
    build_write_frame,
)

# How long to wait for the panel to answer registration with an address.
# Measured on the van: a healthy panel answers in about a second, so this is
# already generous -- it exists to cover a busy panel, not a dead link.
_REGISTER_TIMEOUT = 20  # seconds

# Parameter discovery is sent to each bus device separately (see DEVICE_SEED),
# so the number of frames is a dozen or two rather than two. Nothing is waited
# for in between -- the replies come back as ordinary notifications and are
# handled by the frame handler whenever they land -- so the gap only exists to
# avoid filling the transport queue faster than the panel drains it.
_PARAM_DISC_GAP = 0.15  # seconds
# ...but do wait once after each round, long enough for the replies to arrive,
# because the addresses they carry are what the next round is built from.
_PARAM_DISC_SETTLE = 3  # seconds

# Same reasoning as _PARAM_DISC_GAP -- do not hand the transport a second
# frame before it has drained the first.
_MEASURE_GAP = 0.15  # seconds
# ...and the same settle as _PARAM_DISC_SETTLE, paid only after a measure
# request went unanswered. Ready and DataAck carry no transfer identity, so a
# reply that arrives after we stopped waiting for it would otherwise be taken
# for the next transfer's. The transport invalidates itself over exactly this
# (ble._send_locked) and a probe asks it not to, which makes the wait ours to
# keep. A healthy request pays nothing.
_MEASURE_SETTLE = 3  # seconds
# How many consecutive unanswered requests before a publisher stops being
# asked. A device that has gone away is asked three times a minute apart and
# then left alone; one that is merely slow gets three chances, and one that is
# still publishing anything at all is asked again regardless (see below).
_MEASURE_MISSES_BEFORE_GIVING_UP = 3


class StartupFailed(Exception):
    """The link came up but the session could not be established on it."""


async def run_startup(client, bus: Bus, identity: dict, name: str, now) -> None:
    """Register, subscribe to all topics, send identity, discover params.

    Raises if the panel never assigns us an address. That is the one point
    in startup where the link proves it actually carries traffic, and
    everything after it depends on the answer.
    """
    # 1. Register and wait for an assigned address.
    await client.send(build_register_frame(client.assigned_addr))
    for _ in range(_REGISTER_TIMEOUT):
        await asyncio.sleep(1)
        if client.assigned_addr != DEV_APP_DEFAULT:
            break
        if not client.connected:
            # The transport gave up on this session under us (see
            # TrumaBleClient.send). No address can arrive over a link that
            # is already gone, so hand it back now rather than sitting out
            # the rest of the timeout holding the adapter's slot.
            message = (
                f"Truma {name}: the link ended during "
                "registration; the session is being dropped and retried"
            )
            LOGGER.warning(message)
            raise StartupFailed(message)
    else:
        # Every frame from here on would be sent from the default app
        # address, which the message broker does not route, so carrying on
        # can only produce a session that looks connected and delivers
        # nothing. Measured on the van (2026-09-07 22:09): the link came
        # up, notifications subscribed, and BlueZ then lost the ATT
        # channel -- every write failed with "Service Discovery has not
        # been performed yet", the panel never answered registration, and
        # startup ran to completion anyway. Entities sat blank and
        # "connected" for the full 90 s the stall watchdog takes, and 18
        # parameter-discovery frames were spent on a dead link.
        #
        # Failing here instead hands the link straight back, which also
        # frees the adapter's connection slot for the next attempt.
        #
        # Warn rather than leave it to the session-ended debug line: a
        # link that connects and then carries nothing is the failure
        # people report as "it just stopped working", and it is invisible
        # without this.
        message = (
            f"Truma {name}: the panel assigned us no address "
            f"within {_REGISTER_TIMEOUT}s; the link is up but carries "
            "nothing, so the session is being dropped and retried"
        )
        LOGGER.warning(message)
        raise StartupFailed(message)

    # 2. Subscribe to all topic batches.
    for batch in TOPIC_BATCHES:
        await client.send(build_subscribe_frame(client.assigned_addr, batch))
        await asyncio.sleep(0.5)
    await asyncio.sleep(3)

    # 3. Send the identity sequence.
    for frame in build_identity_frames(client.assigned_addr, identity):
        await client.send(frame)
        await asyncio.sleep(0.5)

    # 4. Request current values from every device on the bus.
    await discover_params(client, bus, name, now)

    # 5. ...and for the sensors that only measure when asked, ask. Step 4
    # returns their last measurement, which on a tank can be hours old, so
    # without this the first reading of every session is stale — and in
    # poll mode, where the link is not held, it would be the only reading.
    await request_measurements(client, bus, name)

async def discover_params(client, bus: Bus, name: str, now) -> None:
    """Ask each bus device for its current parameter values, one by one.

    Asking only the heater and the panel (what this used to do) leaves
    every other device empty until it happens to push a change of its own,
    so tank levels, gas-bottle levels and the mains/battery readings are
    blank after every restart while the panel shows them on its screen.
    A broadcast does not help: nothing but the heater and the panel answers
    one.

    The address list is the seed unioned with whoever has already spoken
    to us, because the seed cannot be authoritative -- devices are
    renumbered when they are re-paired. The replies to the first round
    carry their senders' addresses, so a second round picks up anything
    the seed missed. Each address is asked once: a device that answers
    must not be asked again, or every startup pays for the list twice.
    """
    # Open with one broadcast. Only the heater and the panel answer it,
    # and the directed rounds below cover both -- but it costs a single
    # frame, and an answer from anything else puts that address on the
    # bus, which is how a device outside every seeded class gets asked
    # directly in the second round.
    await client.send(
        build_v3_frame(
            DEV_BROADCAST, client.assigned_addr, CTRL_MBP, MBP_PARAM_DISC, 0, b""
        ),
        probe=True,
    )
    await asyncio.sleep(_PARAM_DISC_GAP)

    asked: set[int] = set()
    acked: set[int] = set()
    started = now()
    for _ in range(2):
        # Measured on the van: the panel sends frames whose src is the
        # address it assigned *us*, so without the last term we ask
        # ourselves for parameters. It is answered like any other address
        # and costs only a frame, which is why it would never be noticed.
        targets = (
            (DEVICE_SEED | bus.addresses)
            - asked
            - {client.assigned_addr}
        )
        if not targets:
            break
        for dev_addr in sorted(targets):
            if await client.send(
                build_v3_frame(
                    dev_addr, client.assigned_addr, CTRL_MBP, MBP_PARAM_DISC, 0, b""
                ),
                probe=True,
            ):
                acked.add(dev_addr)
            asked.add(dev_addr)
            await asyncio.sleep(_PARAM_DISC_GAP)
        await asyncio.sleep(_PARAM_DISC_SETTLE)

    # An address with nothing behind it should cost one frame -- but if the
    # panel withholds the transport acknowledgement for those, each one
    # costs a timeout instead, and the seed becomes a minute of dead time
    # per connect. Report both numbers so that is measurable from a log
    # rather than guessed at.
    # Name the addresses that are not in the seed separately: those are the
    # ones this installation taught us, and seeing them is how a bus the
    # seed does not describe gets reported without asking for a capture.
    LOGGER.debug(
        "Truma %s: parameter discovery asked %d device(s), %d acknowledged, "
        "in %.1fs (learned here: %s) (no ack: %s)",
        name,
        len(asked),
        len(acked),
        now() - started,
        ", ".join(f"0x{a:04X}" for a in sorted(asked - DEVICE_SEED)) or "none",
        ", ".join(f"0x{a:04X}" for a in sorted(asked - acked)) or "none",
    )

    # Not one address answered, and the panel itself is always one of them, so
    # this is not a bus that happens to be empty -- the transport is gone,
    # whatever the link still claims. Registration above guards the same
    # failure one step earlier and for the same reason: a session that carries
    # nothing must not be reported as a working one, or the reconnect backoff
    # resets and _remember_address_kind persists an address kind that has
    # proved nothing. Measured on the van (2026-09-15 11:57): the link dropped
    # during discovery, all 18 addresses went unacknowledged, and startup ran
    # to completion and logged "connected and subscribed" anyway.
    #
    # A *partial* answer is not a failure. An address with nothing behind it is
    # expected to go unacknowledged, which is most of the seed on most
    # vehicles.
    if asked and not acked:
        message = (
            f"Truma {name}: no device acknowledged parameter discovery; "
            "the link is up but carries nothing, so the session is being "
            "dropped and retried"
        )
        LOGGER.warning(message)
        raise StartupFailed(message)

    # Every device on the bus has now been asked to describe itself, and the
    # answers have settled. Said here rather than when the whole startup
    # returns, because what waits on it waits for exactly this: a device that
    # has not named itself by now is not going to (see
    # TrumaCoordinator.device_is_named). The steps after this one can fail on
    # their own, and a startup that got this far and then timed out would
    # otherwise leave the vehicle with values, no names and therefore no
    # entities at all -- where naming those devices after their addresses is
    # the right answer, and was what happened before the wait existed.
    bus.discovered = True


async def request_measurements(client, bus: Bus, name: str) -> None:
    """Ask the on-demand sensors to take a fresh reading.

    A tank sensor reports the level it last measured and nothing else, so
    its value only moves when someone asks it to measure — which the panel
    does when its water screen is opened, and nothing else on the bus does.
    That is the whole of issue #4: the sensor was right, it was answering a
    question asked hours ago.

    A device is asked only once it has reported the topic's own parameter,
    which is the same evidence the entities are created on (see
    ``entity.async_add_rows``). Most vehicles have no tanks at all, and every one
    of them subscribes to these topics regardless, so asking
    unconditionally would put two writes a minute on every bus to answer a
    question nobody had.

    Each publisher is asked separately, and asked at its own address. The
    tanks hang off an electrical block whose address differs per vehicle
    and is renumbered when it is re-paired, so there is no address to
    hard-code — 0x0405 was this reporter's, not anybody's. A vehicle with
    two tank sensors gets two asks rather than one aimed at whichever of
    them spoke last.
    """
    for topic, evidence in MEASURE_REQUEST_TOPICS.items():
        for dest in bus.publishers(topic, evidence):
            device = bus.device(dest)
            missed = bus.measure_misses.get((dest, topic))
            if missed is not None:
                if device.last_seen > missed.heard_at:
                    # It has said something since, so it is there and the
                    # silence was the request's, not the device's.
                    bus.measure_misses.pop((dest, topic), None)
                    missed = None
                elif missed.count >= _MEASURE_MISSES_BEFORE_GIVING_UP:
                    continue
            LOGGER.debug(
                "Truma %s: asking 0x%04X for a fresh %s measurement",
                name,
                dest,
                topic,
            )
            answered = await client.send(
                build_write_frame(
                    client.assigned_addr, dest, topic, MEASURE_REQUEST_PARAM, 1
                ),
                # Silence is one of the answers here. The panel is the peer
                # that acknowledges, and it withholds the acknowledgement for
                # a frame addressed to a device that is not there -- which is
                # the premise discover_params is built on, and the reason it
                # probes too. Without this, one unanswered request invalidates
                # the transport and drops the session: at startup, and then
                # again every _MEASURE_INTERVAL for as long as Home Assistant
                # runs, because the publisher list is everything that has ever
                # reported a level and nothing prunes it. A tank sensor
                # removed or re-paired mid-run would cost a reconnect a
                # minute, with nothing in the log naming the cause.
                #
                # Nothing reads the return value beyond the counter below: a
                # fresh Level arriving through the notification path is what
                # confirms the request, and that is checked by the entity, not
                # here.
                probe=True,
            )
            if answered:
                bus.measure_misses.pop((dest, topic), None)
                await asyncio.sleep(_MEASURE_GAP)
                continue
            count = (missed.count if missed else 0) + 1
            bus.measure_misses[(dest, topic)] = MeasureMiss(count, device.last_seen)
            if count == _MEASURE_MISSES_BEFORE_GIVING_UP:
                # Said once, and out loud: a tank level that has stopped being
                # refreshed still shows its last measurement, so the entity
                # looks fine and the reading is simply old. That is issue #4
                # coming back quietly, and this line is the only thing that
                # would say so.
                LOGGER.warning(
                    "Truma %s: 0x%04X has not answered %d %s measurement "
                    "requests and has published nothing meanwhile; it will "
                    "not be asked again until it does. Its last reading "
                    "stands and will not refresh",
                    name,
                    dest,
                    count,
                    topic,
                )
            else:
                LOGGER.debug(
                    "Truma %s: 0x%04X did not answer a %s measurement request "
                    "(%d in a row)",
                    name,
                    dest,
                    topic,
                    count,
                )
            await asyncio.sleep(_MEASURE_SETTLE)


def handle_frame(bus: Bus, parsed: dict, client=None, name: str = "") -> bool:
    """File one decoded V3 frame into the bus.

    Returns whether anything an entity reads has moved, so that the caller can
    decide whether to notify. A frame that only teaches us what a parameter
    *is* does not move a reading, and a frame that names no source teaches us
    nothing about which device it came from.
    """
    # A frame proves its sender exists at that address, which is how parameter
    # discovery reaches devices no seed could have predicted. Neither
    # pseudo-address is a device, and nor are we: the panel puts our own
    # assigned address in src on some frames.
    src = parsed.get("src")
    if isinstance(src, int) and src not in (
        DEV_BROADCAST,
        DEV_MSG_BROKER,
        bus.assigned_addr,
    ):
        bus.note_seen(src)

    control = parsed.get("control_raw")
    sub_type = parsed.get("sub_type")
    cbor = parsed.get("cbor")
    if not isinstance(cbor, dict):
        return False

    # Registration response -> the address the panel assigned us.
    if control == 0x01 and sub_type == 0x02:
        addr = cbor.get("addr")
        if addr and client is not None:
            client.assigned_addr = addr
            bus.assigned_addr = addr
        return False

    # Info message -> single parameter update.
    if control == 0x03 and sub_type == 0x00:
        tn, pn, v = cbor.get("tn"), cbor.get("pn"), cbor.get("v")
        if tn and pn:
            learn_param(bus, tn, pn, cbor, src, name)
        if tn and pn and v is not None:
            bus.update(tn, pn, v, src)
            return True
        return False

    # Parameter-discovery response -> nested current values.
    if control == 0x03 and sub_type == 0x84:
        for topic in cbor.get("topics", []) or []:
            if not isinstance(topic, dict):
                continue
            tn = topic.get("tn", "")
            for param in topic.get("parameters", []) or []:
                if not isinstance(param, dict):
                    continue
                pn, v = param.get("pn"), param.get("v")
                if tn and pn:
                    learn_param(bus, tn, pn, param, src, name)
                if tn and pn and v is not None:
                    bus.update(tn, pn, v, src)
        return True

    return False


def learn_param(
    bus: Bus, topic: str, param: str, entry: dict, src: int | None, name: str = ""
) -> None:
    """Keep a device's description of a parameter, and log it once.

    A device names its own enum values (see ``Bus.learn_param``), so a question
    like issue #15 -- what does System.FlameStatus == 2 mean on a Combi 6 E,
    when the integration models it as on/off -- is answered by a debug log or a
    diagnostics download rather than by asking somebody to watch their panel
    while their heater ignites.

    Logged only when the description changes, which in practice means once per
    device per parameter per installation: the bus outlives a reconnect, and a
    device describes a parameter the same way every time. The address is in the
    line because two devices describing one parameter differently is the
    interesting case, and without it the two lines would be indistinguishable.
    """
    if bus.learn_param(topic, param, entry, src) and src:
        LOGGER.debug(
            "Truma %s: 0x%04X describes %s.%s as %s",
            name,
            src,
            topic,
            param,
            bus.device(src).meta(topic, param),
        )
