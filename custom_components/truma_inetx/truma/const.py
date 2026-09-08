# BLE UUIDs (all share base -F3B2-11E8-8EB2-F2801F1B9FD1)
# Primary service; advertised even in add-device/pairing mode (when the local
# name may be absent), so it is the most reliable key to find the panel.
SERVICE_UUID = "fc310002-f3b2-11e8-8eb2-f2801f1b9fd1"
CHAR_CMD = "fc314001-f3b2-11e8-8eb2-f2801f1b9fd1"
CHAR_DATA_W = "fc314002-f3b2-11e8-8eb2-f2801f1b9fd1"
CHAR_DATA_R = "fc314003-f3b2-11e8-8eb2-f2801f1b9fd1"
CHAR_CMD_ALT = "fc314004-f3b2-11e8-8eb2-f2801f1b9fd1"  # Do NOT subscribe

# Device addresses
DEV_BROADCAST = 0xFFFF
DEV_MSG_BROKER = 0x0000
DEV_PANEL = 0x0101
DEV_HEATER = 0x0201
DEV_APP_DEFAULT = 0x0500

# Devices to ask for current values at startup even when they have not spoken
# to us yet.
#
# A device address is class << 8 | instance, and only the panel and the heater
# answer a broadcast parameter discovery. Everything else on the bus -- tank
# level sensors, gas-bottle sensors, the electrical block, a roof air
# conditioner -- publishes only when its value *changes*, so after a restart it
# stays silent and its parameters stay empty until someone moves a float or
# flips a switch. The panel meanwhile displays those values happily, which is
# what makes the integration look broken (reported on a Combi 6 E + iNet X Pro
# with two LevelControl sensors, issue #7: the electrical block went from 4 to
# 31 parameters -- FreshWater.Level, GreyWater.Level, LinePower.Plugged, the
# battery voltages -- once it was addressed directly).
#
# Instances are swept rather than listed because they are not stable: a device
# is renumbered when it is re-paired, so the exact addresses seen in one
# installation are a starting point, not a map. Addressing one that is not
# there costs a single frame and no reply -- the panel acknowledges the
# transport, the message broker drops it.
DEVICE_SEED = frozenset(
    {DEV_PANEL, DEV_HEATER}
    # Power and climate: 0x0405 was a Schaudt electrical block, 0x0406 a
    # Dometic FreshJet roof air conditioner.
    | {0x0400 | i for i in range(1, 9)}
    # 0x0603/0x0604 were the left and right gas bottles, 0x0601 the panel's
    # own BLE device management -- so this class is not only level sensors,
    # which is the reason instances are swept rather than guessed at.
    | {0x0600 | i for i in range(1, 9)}
)

# Control types (V3 header byte 6)
CTRL_REGISTRATION = 0x01
CTRL_DISCOVERY = 0x02
CTRL_MBP = 0x03

# MBP sub-types (byte 16 after V3 header)
MBP_INFO = 0x00
MBP_WRITE = 0x01
MBP_SUBSCRIBE = 0x02
MBP_PARAM_DISC = 0x04
MBP_SUBSCRIBE_RESP = 0x82
MBP_PARAM_DISC_RESP = 0x84

# Transport opcodes
TRANSPORT_INIT = 0x01
TRANSPORT_READY = 0x81
TRANSPORT_ACK = 0xF0
TRANSPORT_MSG_ACK = 0x83
TRANSPORT_CONFIRM = 0x03

# Topic subscription batches (10 per batch, per protocol spec)
TOPIC_BATCHES = [
    ["AirCirculation", "AirCooling", "AirHeating", "DeviceManagement",
     "EnergySrc", "ErrorReset", "FreshWater", "GasBtl", "GasControl", "GreyWater"],
    ["Identify", "L1Bat", "L2Bat", "LinePower", "MobileIdentity",
     "PowerSupply", "RoomClimate", "Switches", "Temperature", "Transfer"],
    ["VBat", "WaterHeating", "AmbientLight", "Panel", "BatteryMngmt",
     "Install", "Connect", "TimerConfig", "BleDeviceManagement", "BluetoothDevice"],
    ["System", "Resources", "PowerMgmt"],
]

ADAPTER_PATH = "/org/bluez/hci1"
IDENTITY_FILE = "/data/dbus-truma/.truma_identity.json"
BLUEZ = "org.bluez"

# Topics whose device measures on demand rather than continuously.
#
# A tank sensor answers with the level it measured when it was last asked, not
# the level now. The panel hides this because it asks for a fresh measurement
# whenever its water screen is opened -- so the panel is right and everyone
# reading the bus is stale. Reported on a Weinsberg (issue #4): a grey tank
# emptied by hand still read 25 % afterwards, and a parameter discovery
# returned that same 25 % because it is genuinely the last measurement taken.
#
# Asking is a write of 1 to the topic's MeasureRequest parameter; the device
# answers with a fresh Level a moment later, through the ordinary
# notification path.
#
# The value is the parameter whose arrival proves the hardware exists, so a
# vehicle with no tanks is never asked -- most have neither, and the topics
# are subscribed on every vehicle regardless.
MEASURE_REQUEST_TOPICS = {
    "FreshWater": "Level",
    "GreyWater": "Level",
}
MEASURE_REQUEST_PARAM = "MeasureRequest"
