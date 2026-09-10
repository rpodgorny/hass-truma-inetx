from enum import IntEnum
from dataclasses import dataclass, field
from typing import Optional, Any
import time


class RoomClimateMode(IntEnum):
    OFF = 0
    HEATING = 3
    VENTILATING = 5


class WaterHeatingMode(IntEnum):
    TEMP_40 = 0  # 40°C
    TEMP_60 = 1  # 60°C
    TEMP_70 = 2  # 70°C


class ActiveState(IntEnum):
    OFF = 0
    ACTIVE = 1
    IDLE = 2


class FanMode(IntEnum):
    FAST = 0
    COMFORT = 1


class DieselLevel(IntEnum):
    OFF = 0
    ON = 1


class ElectricLevel(IntEnum):
    OFF = 0
    W900 = 1   # 900W
    W1800 = 2  # 1800W


# Command routing: topic -> destination device address.
#
# Only topics whose owner really is fixed belong here. Everything else is
# addressed to whoever reported it; see get_command_dest.
#
# AirCooling used to be in this table, addressed to the heater -- which on the
# vehicles that actually have cooling is not the device that does it. Measured
# on a Combi 6 E with a Dometic FreshJet 2200 at 0x0406 (#10): a write of
# AirCooling.TgtTemp to 0x0201 is acknowledged by the transport and then
# silently dropped -- no error, nothing cools -- while the same write to
# 0x0406 is acknowledged and the roof unit starts. Hard-coding 0x0406 would be
# as wrong as hard-coding the heater, because a device is renumbered when it is
# re-paired, so the topic's own reporter is the only durable answer.
COMMAND_DEST = {
    "RoomClimate": 0x0101,    # panel
    "AirHeating": 0x0201,     # heater
    "WaterHeating": 0x0201,   # heater
    "AirCirculation": 0x0201, # heater
    "EnergySrc": 0x0201,      # heater
}

# Command validation: topic.param -> (min, max) or list of valid values
PARAM_VALIDATION = {
    "RoomClimate.Mode": [0, 3, 5],
    "RoomClimate.TgtTemp": (160, 300),  # wire values
    "AirHeating.TgtTemp": (50, 300),
    "AirHeating.Mode": [0, 1],
    "AirCirculation.FanLevel": (0, 10),
    "AirCirculation.Active": [0, 1],
    "WaterHeating.Mode": [0, 1, 2],
    "WaterHeating.Active": [0, 1],
    "WaterHeating.BoostMode": [0, 1],
    "WaterHeating.FasterHeatingMode": [0, 1],
    "EnergySrc.DieselLevel": [0, 1],
    "EnergySrc.ElectricLevel": [0, 1, 2],
    "Switches.FreshWaterPump": [0, 1],
}

# The keys a parameter's own description arrives under, alongside "tn"/"pn"/
# "v". Panels send them on plain value updates as well as in the answer to a
# discovery request, so both paths feed TrumaState.learn_param().
_PARAM_META_KEYS = ("type", "perm", "avail", "min", "max")

_TOPIC_PARAM_MAP = {
    ("RoomClimate", "Mode"): "room_mode",
    ("RoomClimate", "TgtTemp"): "room_target_temp",
    ("RoomClimate", "Active"): "room_active",
    ("AirHeating", "TgtTemp"): "air_target_temp",
    ("AirHeating", "Temp"): "air_current_temp",
    ("AirHeating", "Mode"): "air_mode",
    ("AirHeating", "Active"): "air_active",
    ("AirCirculation", "FanLevel"): "fan_level",
    ("AirCirculation", "Active"): "fan_active",
    ("WaterHeating", "Mode"): "water_mode",
    ("WaterHeating", "Active"): "water_active",
    ("WaterHeating", "Temp"): "water_current_temp",
    ("WaterHeating", "BoostMode"): "water_boost",
    ("WaterHeating", "FasterHeatingMode"): "water_faster_heating",
    ("WaterHeating", "FasterHeatingModeTime"): "water_faster_heating_time",
    ("EnergySrc", "DieselLevel"): "diesel_level",
    ("EnergySrc", "ElectricLevel"): "electric_level",
    ("EnergySrc", "GasLevel"): "gas_level",
    ("System", "FlameStatus"): "flame_status",
    ("Eol", "Vcc12"): "voltage_vcc12",
    ("ErrorReset", "ErrCode"): "error_codes",
    ("Panel", "UserInactiveSince"): "panel_inactive_since",
    ("Temperature", "Internal"): "internal_temp",
    ("Switches", "FreshWaterPump"): "water_pump",
    ("FreshWater", "Level"): "fresh_water_level",
    ("GreyWater", "Level"): "grey_water_level",
    ("VBat", "Voltage"): "starter_battery_voltage",
    ("L1Bat", "Voltage"): "leisure_battery_voltage",
}


@dataclass
class TrumaState:
    """Current state of Truma heater system."""
    # Room climate
    room_mode: Optional[int] = None
    room_target_temp: Optional[int] = None  # wire value (tenths of C)
    room_current_temp: Optional[int] = None  # wire value
    room_active: Optional[int] = None

    # Air heating
    air_target_temp: Optional[int] = None
    air_current_temp: Optional[int] = None
    air_mode: Optional[int] = None  # fast/comfort
    air_active: Optional[int] = None

    # Air circulation
    fan_level: Optional[int] = None
    fan_active: Optional[int] = None

    # Water heating
    water_mode: Optional[int] = None
    water_active: Optional[int] = None
    water_current_temp: Optional[int] = None
    # The panel's two ways of putting the water first, from the
    # reverse-engineered schema in daaaaan/truma-inetx-ble: both documented as
    # 0/1 and nothing more, with the timed one carrying a duration in seconds
    # beside it. Which of the two the panel's "boost" writes is unmeasured, so
    # both are carried and each entity waits for its own parameter -- a vehicle
    # that reports neither is given neither.
    water_boost: Optional[int] = None
    water_faster_heating: Optional[int] = None
    water_faster_heating_time: Optional[int] = None  # seconds

    # Energy
    diesel_level: Optional[int] = None
    electric_level: Optional[int] = None
    # Gas is reflected, never commanded (#16). On a gas/electric Combi the
    # heater moves this itself -- switching the electric element off was
    # measured turning the gas source on with nothing written from here -- so a
    # switch modelling it as the user's to own would fight the heater and flap.
    gas_level: Optional[int] = None

    # System
    flame_status: Optional[int] = None
    voltage_vcc12: Optional[int] = None  # millivolts
    error_codes: Optional[list] = None

    # Panel
    panel_inactive_since: Optional[int] = None

    # Internal temp
    internal_temp: Optional[int] = None

    # Water. The tanks and the pump belong to whichever device owns them --
    # an electrical block on the vehicles seen so far, not the heater and not
    # the panel. Levels are a percentage the sensor reports in quarter steps
    # (0/25/50/75/100), so there is nothing to scale and nothing below the
    # decimal point.
    water_pump: Optional[int] = None
    fresh_water_level: Optional[int] = None
    grey_water_level: Optional[int] = None

    # Vehicle batteries, reported by the electrical block rather than by the
    # heater: VBat is the starter battery, L1Bat the leisure one (#17). Both
    # arrive in tenths of a volt -- 137 is 13.7 V -- which is a different scale
    # from Eol.Vcc12 above, and the reason they are kept as separate fields
    # rather than folded into the supply-voltage sensor.
    starter_battery_voltage: Optional[int] = None
    leisure_battery_voltage: Optional[int] = None

    # Metadata
    last_update: float = 0.0
    connected: bool = False
    assigned_addr: int = 0x0500

    # Raw storage for debugging
    raw_params: dict = field(default_factory=dict)

    # "Topic.Param" -> what the panel says the parameter *is*, as opposed to
    # what it currently reads: its range, and for an enum the panel's own name
    # for every value. This is the only documentation of the protocol that
    # exists -- Truma publishes none -- and it arrives free, beside each value.
    # Issue #15 is what happens without it: System.FlameStatus takes 0, 1 and 2
    # on a Combi 6 E, is modelled as a binary sensor, and whatever 2 means is
    # folded into on or off because nobody knows which it is.
    #
    # Deliberately not wired into any entity: it is evidence to read in a
    # diagnostics download, not a schema to start behaving differently on.
    param_meta: dict = field(default_factory=dict)

    # Every source address that has sent us a frame. Parameter discovery is
    # addressed device by device (see DEVICE_SEED), and a device that has
    # spoken once is proof its address exists -- which matters because
    # addresses are renumbered on re-pairing, so no fixed list can be right for
    # every installation.
    #
    # The coordinator holds one state object across reconnects, so this
    # accumulates: a device that turns up late in one session -- battery-
    # powered gas sensors were measured taking minutes -- is asked directly
    # from the next connect onwards.
    seen_devices: set = field(default_factory=set)

    # Topic name -> the device address that last reported it. A write has to
    # reach the device that owns the topic, and for anything outside the fixed
    # table below there is no way to know that in advance: the fresh-water
    # pump sits on an electrical block whose address differs per vehicle and
    # is renumbered when it is re-paired. Whoever reports a topic is the
    # authority on where a write to it should go.
    topic_source: dict = field(default_factory=dict)

    # The same values as raw_params, kept under the address that sent them:
    # ``{0x0201: {"AirCirculation.FanLevel": 4}, 0x0406: {...}}``.
    #
    # A topic is a message class, not a device. Subscription names topics and
    # is addressed to the broker, so every device implementing a topic
    # publishes under it and the only thing telling two publishers apart is
    # the src address in the frame header. Two instances of one device class
    # therefore share every key: a Combi and a roof air conditioner both
    # report AirCirculation.FanLevel, and a pair of gas-bottle sensors at
    # 0x0603/0x0604 share the whole of GasBtl.
    #
    # Flattened into raw_params they overwrite each other, and #9 is what that
    # looks like on a vehicle: not a merely stale reading, but GasBtl.Name
    # from one bottle standing beside GasBtl.FillLevelP from the other in the
    # same record, reading entirely plausibly -- 49 % under the name of the
    # bottle that is full. Identify goes the same way, because every device on
    # the bus describes itself under it, so the panel's own name and serial
    # end up being whichever sensor spoke last.
    #
    # raw_params stays as the flat view the entities already read; it is right
    # wherever only one device reports a topic, which is most of them. This is
    # the one to read wherever more than one device can answer.
    device_params: dict = field(default_factory=dict)

    def update(
        self, topic: str, param: str, value: Any, src: Optional[int] = None
    ) -> None:
        """Update state from a decoded BLE notification.

        ``src`` is the V3 header's source address, i.e. the device that sent
        this value. It is remembered per topic so a write can be addressed
        back to it (see :meth:`get_command_dest`), and the value is filed
        under it as well, so that two devices reporting one topic do not
        overwrite each other (see :attr:`device_params`).
        """
        self.last_update = time.time()
        self.raw_params[f"{topic}.{param}"] = value
        # 0 is the message broker, not a device: it must not be learned as a
        # write destination, and it owns no parameters either. Both stores are
        # fed on the same condition, so whatever the flat view holds is always
        # attributable in the per-device one.
        if isinstance(src, int) and src:
            self.topic_source[topic] = src
            self.device_params.setdefault(src, {})[f"{topic}.{param}"] = value

        # Convert value to int if possible
        v = int(value) if isinstance(value, (int, float)) else value

        field_name = _TOPIC_PARAM_MAP.get((topic, param))
        if field_name and isinstance(v, int):
            setattr(self, field_name, v)

    def device_param(self, addr: int, topic: str, param: str) -> Any:
        """What one device reports for a parameter, or None if it has not.

        raw_params is last-writer-wins across the whole bus, which is correct
        only while a single device reports the topic. Where two can -- a
        Combi's AirCirculation.FanLevel against a roof air conditioner's,
        anything under GasBtl on a vehicle with two bottle sensors, Identify
        against every device that has one -- this is the only reading that
        means what it says.
        """
        return self.device_params.get(addr, {}).get(f"{topic}.{param}")

    def learn_param(self, topic: str, param: str, entry: Any) -> bool:
        """Record what the panel says about a parameter. True if that is new.

        Every value the panel sends is wrapped in a description of the
        parameter carrying it: ``type``, ``perm`` (writable?), ``avail``,
        ``min``, ``max``, and ``enum`` -- a list of ``{"n": name, "a":
        available, "v": value}`` in which the panel names each value itself.
        All of it used to be dropped, which is why the meaning of a value has
        had to be measured on somebody's vehicle instead of read off the bus.

        ``a`` is per-installation, not per-protocol: a heater without a diesel
        burner still gets an enum that names the diesel value, marked
        unavailable. That distinction is worth keeping -- it says which values
        a given vehicle can actually produce.
        """
        if not isinstance(entry, dict):
            return False

        meta: dict = {
            key: entry[key] for key in _PARAM_META_KEYS if entry.get(key) is not None
        }

        elements = entry.get("enum")
        if isinstance(elements, list):
            names: dict = {}
            unavailable: list = []
            for element in elements:
                if not isinstance(element, dict):
                    continue
                value, name = element.get("v"), element.get("n")
                if not isinstance(value, int) or name is None:
                    continue
                names[str(value)] = str(name)
                if not element.get("a", True):
                    unavailable.append(str(value))
            if names:
                meta["enum"] = names
            if unavailable:
                meta["enum_unavailable"] = unavailable

        if not meta:
            return False

        # Merge rather than replace. A plain value update may describe less
        # than the answer to a discovery request did, and dropping the enum
        # again on the next frame would defeat the whole point.
        known = self.param_meta.setdefault(f"{topic}.{param}", {})
        changed = any(known.get(key) != value for key, value in meta.items())
        known.update(meta)
        return changed

    def allowed_values(self, topic: str, param: str) -> Optional[list]:
        """The values the panel says *this* vehicle can be set to, or None.

        A panel enumerates a parameter per installation, not per protocol. The
        van this was measured on has no air conditioner, and its
        ``RoomClimate.Mode`` enum is simply ``{0: Off, 3: Heating,
        5: Ventilating}`` -- 1 and 2 are absent rather than present-and-
        unavailable. A Combi 6 E reports automatic and cooling there (#11).
        Which is why no list written into this file can be right for both, and
        why the panel is asked instead.

        ``None`` means the panel described no enum for the parameter, and the
        caller's own table is all there is.

        Only the values are returned, never the panel's names for them. Those
        arrive in the panel's display language -- the same three water-heating
        steps come back as ``40 / 60 / 70`` here and as Eco / Comfort / Hot on
        the Combi 6 E in #12 -- so they are evidence about which value means
        what, and they have no business reaching a user-facing string.
        """
        meta = self.param_meta.get(f"{topic}.{param}")
        names = meta.get("enum") if meta else None
        if not names:
            return None
        unavailable = set(meta.get("enum_unavailable", ()))
        values = [
            int(value)
            for value in names
            if value not in unavailable and str(value).lstrip("-").isdigit()
        ]
        return sorted(values) or None

    def validate_write(self, topic: str, param: str, value: int) -> tuple:
        """Validate a write, preferring what the panel enumerated to our table.

        ``PARAM_VALIDATION`` is a guess assembled from the vehicles reported so
        far, and it rejects what it has not seen: a Combi 6 E cannot be put
        into automatic or cooling through this integration because ``[0, 3, 5]``
        says those do not exist (#11). The panel's own enum is that vehicle's
        answer to the same question, so where there is one, it wins.
        """
        allowed = self.allowed_values(topic, param)
        if allowed is None:
            return self.validate_command(topic, param, value)
        if value not in allowed:
            return False, f"{topic}.{param}: the panel offers only {allowed}"
        return True, "ok"

    @staticmethod
    def wire_to_celsius(wire_value: Optional[int]) -> Optional[float]:
        """Convert wire value (tenths of C) to Celsius."""
        if wire_value is None:
            return None
        return wire_value / 10.0

    def get_status(self) -> dict:
        """Get full status as dict for REST API / JSON serialization."""
        # Room climate section — include if any room data present
        if self.room_mode is not None:
            room_mode_name = (
                RoomClimateMode(self.room_mode).name
                if self.room_mode in (0, 3, 5)
                else str(self.room_mode)
            )
            room_active_name = (
                ActiveState(self.room_active).name
                if self.room_active in (0, 1, 2)
                else str(self.room_active)
            ) if self.room_active is not None else None
            room_climate = {
                "mode": self.room_mode,
                "mode_name": room_mode_name,
                "target_temp_c": self.wire_to_celsius(self.room_target_temp),
                "current_temp_c": self.wire_to_celsius(self.air_current_temp),
                "active": self.room_active,
                "active_name": room_active_name,
            }
        else:
            room_climate = None

        # Water heating section
        if self.water_mode is not None or self.water_current_temp is not None:
            water_mode_name = (
                WaterHeatingMode(self.water_mode).name
                if self.water_mode in (0, 1, 2)
                else str(self.water_mode)
            ) if self.water_mode is not None else None
            water_active_name = (
                ActiveState(self.water_active).name
                if self.water_active in (0, 1, 2)
                else str(self.water_active)
            ) if self.water_active is not None else None
            water_heating = {
                "mode": self.water_mode,
                "mode_name": water_mode_name,
                "active": self.water_active,
                "active_name": water_active_name,
                "current_temp_c": self.wire_to_celsius(self.water_current_temp),
                "boost": self.water_boost,
                "faster_heating": self.water_faster_heating,
                "faster_heating_time_s": self.water_faster_heating_time,
            }
        else:
            water_heating = None

        # Air heating section
        if self.air_current_temp is not None:
            air_mode_name = (
                FanMode(self.air_mode).name
                if self.air_mode in (0, 1)
                else str(self.air_mode)
            ) if self.air_mode is not None else None
            air_heating = {
                "target_temp_c": self.wire_to_celsius(self.air_target_temp),
                "current_temp_c": self.wire_to_celsius(self.air_current_temp),
                "mode": self.air_mode,
                "mode_name": air_mode_name,
                "active": self.air_active,
                "fan_level": self.fan_level,
            }
        else:
            air_heating = None

        # Energy section
        if (
            self.diesel_level is not None
            or self.electric_level is not None
            or self.gas_level is not None
        ):
            diesel_name = (
                DieselLevel(self.diesel_level).name
                if self.diesel_level in (0, 1)
                else str(self.diesel_level)
            ) if self.diesel_level is not None else None
            electric_name = (
                ElectricLevel(self.electric_level).name
                if self.electric_level in (0, 1, 2)
                else str(self.electric_level)
            ) if self.electric_level is not None else None
            energy = {
                "diesel": self.diesel_level,
                "diesel_name": diesel_name,
                "electric": self.electric_level,
                "electric_name": electric_name,
                # No name for gas: the panel enumerates DieselLevel as
                # "Diesel off"/"Diesel on" and nothing has yet shown what it
                # calls the gas values, so inventing a pair here would be
                # dressing a guess up as a reading.
                "gas": self.gas_level,
            }
        else:
            energy = None

        return {
            "connected": self.connected,
            "last_update": self.last_update,
            "assigned_addr": f"0x{self.assigned_addr:04X}",
            "room_climate": room_climate,
            "water_heating": water_heating,
            "air_heating": air_heating,
            "energy": energy,
            "system": {
                "flame_status": self.flame_status,
                "voltage_v": (self.voltage_vcc12 / 1000.0) if self.voltage_vcc12 is not None else None,
                "internal_temp_c": self.wire_to_celsius(self.internal_temp),
                "error_codes": self.error_codes,
            },
        }

    @staticmethod
    def validate_command(topic: str, param: str, value: int) -> tuple:
        """Validate a command before sending.

        Returns (ok: bool, message: str).
        """
        key = f"{topic}.{param}"
        rule = PARAM_VALIDATION.get(key)
        if rule is None:
            return True, "ok"  # unknown param, allow
        if isinstance(rule, list):
            if value not in rule:
                return False, f"{key}: value {value} not in {rule}"
        elif isinstance(rule, tuple):
            if value < rule[0] or value > rule[1]:
                return False, f"{key}: value {value} not in range {rule[0]}-{rule[1]}"
        return True, "ok"

    def get_command_dest(self, topic: str) -> int:
        """Get destination device address for a command topic.

        The fixed table wins wherever it has an entry. Those destinations are
        measured, and a topic the panel relays on our behalf has to keep going
        to the panel however the value reaches us.

        Everything else is addressed to whoever last reported it. That is the
        only workable answer for the water topics, whose owning device has no
        fixed address, and it degrades to the panel when nothing has reported
        the topic yet -- which is the behaviour this replaced.
        """
        dest = COMMAND_DEST.get(topic)
        if dest is not None:
            return dest
        return self.topic_source.get(topic, 0x0101)  # default to panel
