"""Truma iNetX V3 frame building and parsing.

Ported from scripts/test_protocol.py with the proven correct implementation.
"""

import struct
import time
import cbor2

from .const import (
    CTRL_REGISTRATION, CTRL_MBP,
    MBP_WRITE, MBP_SUBSCRIBE,
    DEV_BROADCAST, DEV_MSG_BROKER, DEV_PANEL,
)


def build_v3_frame(dest, src, ctrl, sub_type, corr_id, cbor_payload):
    """Build a TruMessageV3 frame per protocol spec.

    Layout:
    [0-1]  dest device ID (UShort LE)
    [2-3]  src device ID (UShort LE)
    [4-5]  packet_size (UShort LE) = payload_len + 9
    [6]    control_type
    [7-15] segmentation header (9 bytes, all zero for non-segmented)
    [16]   sub_type (MBP type / reg type)
    [17]   correlation ID
    [18+]  CBOR payload
    """
    seg_header = bytes(9)  # no segmentation
    sub_header = bytes([sub_type, corr_id])
    payload = sub_header + cbor_payload
    packet_size = len(payload) + 9  # payload + segmentation header size

    header = struct.pack('<HH', dest, src)
    header += struct.pack('<H', packet_size)
    header += bytes([ctrl])
    header += seg_header
    header += payload

    return header


def parse_v3_frame(data):
    """Parse a TruMessageV3 frame, return dict or None.

    Returns dict with keys:
        dest, src, pkt_size, control, control_raw,
        sub_type, corr_id, cbor (decoded dict or None)
    """
    if len(data) < 16:
        return None

    dest = struct.unpack_from('<H', data, 0)[0]
    src = struct.unpack_from('<H', data, 2)[0]
    pkt_size = struct.unpack_from('<H', data, 4)[0]
    control = data[6]

    ctrl_names = {
        0x01: 'REGISTRATION', 0x02: 'DISCOVERY', 0x03: 'MBP',
        0x04: 'FILE_MANAGER', 0x05: 'SECURITY', 0x06: 'FIRMWARE', 0x0A: 'NONE'
    }

    result = {
        'dest': dest,
        'src': src,
        'pkt_size': pkt_size,
        'control': ctrl_names.get(control, f'0x{control:02X}'),
        'control_raw': control,
    }

    if len(data) > 16:
        sub_type = data[16]
        corr_id = data[17] if len(data) > 17 else 0
        result['sub_type'] = sub_type
        result['corr_id'] = corr_id

        # Try CBOR decode at offset 18
        cbor_decoded = None
        if len(data) > 18:
            try:
                cbor_decoded = cbor2.loads(data[18:])
                if not isinstance(cbor_decoded, dict):
                    cbor_decoded = None
            except Exception:
                cbor_decoded = None
        result['cbor'] = cbor_decoded

    return result


def build_register_frame(src):
    """Build CTRL_REGISTRATION request frame.

    CBOR: {"pv": [5, 1]}, ctrl=0x01, sub=0x01, corr=0x42, dest=0xFFFF
    """
    cbor_payload = cbor2.dumps({'pv': [5, 1]})
    return build_v3_frame(
        dest=DEV_BROADCAST,
        src=src,
        ctrl=CTRL_REGISTRATION,
        sub_type=0x01,
        corr_id=0x42,
        cbor_payload=cbor_payload,
    )


def build_subscribe_frame(src, topics):
    """Build MBP_SUBSCRIBE frame for a list of topic names.

    CBOR: {"tn": topics}, ctrl=0x03, sub=0x02, dest=0x0000
    """
    cbor_payload = cbor2.dumps({'tn': topics})
    return build_v3_frame(
        dest=DEV_MSG_BROKER,
        src=src,
        ctrl=CTRL_MBP,
        sub_type=MBP_SUBSCRIBE,
        corr_id=0,
        cbor_payload=cbor_payload,
    )


def build_write_frame(src, dest, topic, param, value):
    """Build MBP_WRITE frame for a single parameter write.

    CBOR: {"tn": topic, "pn": param, "v": value, "id": 0}
    """
    cbor_payload = cbor2.dumps({'tn': topic, 'pn': param, 'v': value, 'id': 0})
    return build_v3_frame(
        dest=dest,
        src=src,
        ctrl=CTRL_MBP,
        sub_type=MBP_WRITE,
        corr_id=0,
        cbor_payload=cbor_payload,
    )


def build_identity_frames(src, identity):
    """Build the identity + system time init sequence.

    Returns a list of (frame_bytes, delay_after) tuples where delay_after
    is a float in seconds to pause after sending that frame.

    Sequence:
        SystemTime/Time, SystemTime/Lot,
        MobileIdentity/UserName, MobileIdentity/Muid+Uuid,
        LastMessage
    """
    frames = []

    # SystemTime
    frames.append(build_write_frame(src, DEV_PANEL, 'SystemTime', 'Time', int(time.time())))
    frames.append(build_write_frame(src, DEV_PANEL, 'SystemTime', 'Lot', 0))

    # MobileIdentity — UserName
    frames.append(build_write_frame(src, DEV_PANEL, 'MobileIdentity', 'UserName', identity['username']))

    # MobileIdentity — Muid
    frames.append(build_write_frame(src, DEV_PANEL, 'MobileIdentity', 'Muid', identity['muid']))

    # MobileIdentity — Uuid
    frames.append(build_write_frame(src, DEV_PANEL, 'MobileIdentity', 'Uuid', identity['uuid']))

    # LastMessage sentinel
    cbor_payload = cbor2.dumps({'LastMessage': 1})
    frames.append(build_v3_frame(
        dest=DEV_PANEL,
        src=src,
        ctrl=CTRL_MBP,
        sub_type=MBP_WRITE,
        corr_id=0,
        cbor_payload=cbor_payload,
    ))

    return frames
