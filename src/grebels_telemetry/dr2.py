"""Codemasters "DiRT Rally 2.0 / extradata=3" UDP telemetry packet.

264 bytes: 66 little-endian float32. SimHub (and most motion software) will
accept this on a UDP port without the real game existing anywhere, which is why
it is the pragmatic transport for a game nobody supports natively.

Field indices are the documented Codemasters layout. The ones that matter here:

    4-6    position x, y, z          (metres, X right / Y up / Z forward)
    7      speed                     (m/s)
    8-10   velocity vector
    11-13  "roll vector"  -- the craft's RIGHT axis, unit length
    14-16  "pitch vector" -- the craft's NOSE axis, unit length
    34-35  lateral / longitudinal G

SimHub reconstructs pitch, roll and yaw from those two orientation vectors, so
they matter more than anything else in the packet. Everything car-shaped
(suspension, wheels, clutch) is left at zero.
"""

import math
import struct

FLOAT_COUNT = 66
PACKET_SIZE = FLOAT_COUNT * 4
DEFAULT_PORT = 20777

_PACKER = struct.Struct("<%df" % FLOAT_COUNT)


def build_packet(time_s, pos, velocity, speed, right_axis, nose_axis,
                 g_lateral, g_longitudinal, rpm=3000.0, throttle=0.0,
                 brake=0.0, gear=1.0, max_rpm=8000.0, distance=0.0):
    f = [0.0] * FLOAT_COUNT
    f[0] = time_s                       # total time
    f[1] = time_s                       # lap time
    f[2] = distance                     # lap distance
    f[3] = distance                     # total distance
    f[4], f[5], f[6] = pos
    f[7] = speed
    f[8], f[9], f[10] = velocity
    f[11], f[12], f[13] = right_axis
    f[14], f[15], f[16] = nose_axis
    f[29] = throttle
    f[31] = brake
    f[33] = gear
    f[34] = g_lateral
    f[35] = g_longitudinal
    f[36] = 1.0                         # current lap
    f[37] = rpm / 10.0                  # engine rate is rpm/10
    f[60] = 1.0                         # total laps
    f[61] = 10000.0                     # track length
    f[63] = max_rpm / 10.0
    return _PACKER.pack(*f)


def parse_packet(data):
    """Unpack for tests and diagnostics."""
    if len(data) != PACKET_SIZE:
        raise ValueError("expected %d bytes, got %d" % (PACKET_SIZE, len(data)))
    f = _PACKER.unpack(data)
    return {
        "time": f[0], "position": f[4:7], "speed": f[7], "velocity": f[8:11],
        "right_axis": f[11:14], "nose_axis": f[14:17],
        "g_lateral": f[34], "g_longitudinal": f[35], "rpm": f[37] * 10.0,
    }


# ---------------------------------------------------------------- geometry --
def unreal_rotator_to_axes(pitch_deg, yaw_deg, roll_deg):
    """FRotator -> (forward, right) unit axes, in Unreal space.

    This is Unreal's own FRotationMatrix, spelled out.
    """
    p, y, r = math.radians(pitch_deg), math.radians(yaw_deg), math.radians(roll_deg)
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    forward = (cp * cy, cp * sy, sp)
    right = (sr * sp * cy - cr * sy,
             sr * sp * sy + cr * cy,
             -sr * cp)
    return forward, right


def unreal_to_packet_space(v):
    """Unreal (X forward, Y right, Z up) -> packet (X right, Y up, Z forward)."""
    return (v[1], v[2], v[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def magnitude(v):
    return math.sqrt(dot(v, v))
