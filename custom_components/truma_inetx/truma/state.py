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


# Command routing: topic -> destination device address
COMMAND_DEST = {
    "RoomClimate": 0x0101,    # panel
    "AirHeating": 0x0201,     # heater
    "WaterHeating": 0x0201,   # heater
    "AirCirculation": 0x0201, # heater
    "AirCooling": 0x0201,     # heater
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
    "EnergySrc.DieselLevel": [0, 1],
    "EnergySrc.ElectricLevel": [0, 1, 2],
}

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
    ("EnergySrc", "DieselLevel"): "diesel_level",
    ("EnergySrc", "ElectricLevel"): "electric_level",
    ("System", "FlameStatus"): "flame_status",
    ("Eol", "Vcc12"): "voltage_vcc12",
    ("ErrorReset", "ErrCode"): "error_codes",
    ("Panel", "UserInactiveSince"): "panel_inactive_since",
    ("Temperature", "Internal"): "internal_temp",
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

    # Energy
    diesel_level: Optional[int] = None
    electric_level: Optional[int] = None

    # System
    flame_status: Optional[int] = None
    voltage_vcc12: Optional[int] = None  # millivolts
    error_codes: Optional[list] = None

    # Panel
    panel_inactive_since: Optional[int] = None

    # Internal temp
    internal_temp: Optional[int] = None

    # Metadata
    last_update: float = 0.0
    connected: bool = False
    assigned_addr: int = 0x0500

    # Raw storage for debugging
    raw_params: dict = field(default_factory=dict)

    def update(self, topic: str, param: str, value: Any) -> None:
        """Update state from a decoded BLE notification."""
        self.last_update = time.time()
        self.raw_params[f"{topic}.{param}"] = value

        # Convert value to int if possible
        v = int(value) if isinstance(value, (int, float)) else value

        field_name = _TOPIC_PARAM_MAP.get((topic, param))
        if field_name and isinstance(v, int):
            setattr(self, field_name, v)

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
        if self.diesel_level is not None or self.electric_level is not None:
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

    @staticmethod
    def get_command_dest(topic: str) -> int:
        """Get destination device address for a command topic."""
        return COMMAND_DEST.get(topic, 0x0101)  # default to panel
