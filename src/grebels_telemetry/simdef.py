"""SimHub "External Sim" native telemetry packet for G-Rebels.

The wire format is not ours to choose: SimHub generates a C# struct from the
.simdef definition, and this must match it byte for byte. Layout is Pack=1
(no padding), little endian, 55-byte header plus 33 fields of 4 bytes = 187.

Seven of those fields are SimHub *standard* fields, which is what makes the
Motion plugin work at all -- custom fields are exposed as properties but are
not fed into SimHub's internal telemetry model. The standard fields arrive with
conventions that differ from Unreal's, and getting them wrong fails silently:
the platform still moves, just incorrectly. See UNREAL_TO_SIMHUB below.

Reordering or removing fields changes TelemetrySignature and breaks the
contract. Appending is a minor-version change and is safe.
"""

import os
import random
import socket
import struct

# -- generated constants (from SHTelemetryConstants) -------------------------
DEFINITION_UNIQUE_ID = "971ee505-8741-4fe4-9dc8-9df425e07242"
GAME_SIGNATURE = 0x18B2ACA6
TELEMETRY_SIGNATURE = 0x3C4D8E8D
LAYOUT_MAJOR = 1
LAYOUT_MINOR = 0
EXPECTED_PACKET_LENGTH = 211
DEFAULT_PORT = 30777

GRAVITY = 9.80665
MS_TO_KMH = 3.6

# "<" means little endian AND no alignment padding, which is what Pack=1 is.
_HEADER = ("<"
           "I"    # GameSignature
           "I"    # TelemetrySignature
           "H"    # LayoutMajorVersion
           "H"    # LayoutMinorVersion
           "Q"    # EmitterInstanceId
           "B"    # PacketId
           "Q"    # PacketsCounter
           "B"    # IsSessionRunning
           "B"    # IsSessionPaused
           "Q"    # SessionId
           "B"    # IsReplay
           "B"    # IsUserInControl
           "B"    # IsAIInControl
           "B"    # IsSpectator
           "d"    # SessionTimeSeconds
           "I")   # PhysicsDiscontinuityCounter

_FIELDS = ("i"     # time_ms
           "fff"   # position_x/y/z
           "fff"   # velocity_x/y/z
           "fff"   # PitchDegrees/YawDegrees/RollDegrees   [standard]
           "f"     # SpeedKmh                              [standard]
           "f"     # altitude
           "fff"   # LocalSurge/Sway/HeaveMs2              [standard]
           "ff"    # rounds_fired, fire_impulse
           "ff"    # ammo_primary, ammo_max
           "f"     # heat_primary
           "ii"    # is_overheated, shoot_left
           "ff"    # missiles_fired, missiles_available
           "i"     # missile_warning
           "fff"   # health, shield, shield_max
           "f"     # boost_axis
           "i"     # boost_active
           "f"     # boost_time_pct
           "ii"    # landing_gear, is_landing
           "ff"    # EngineRpm, EngineMaxRpm                 [standard]
           "ff"    # Throttle, Brake                         [standard]
           "8s")   # Gear -- UTF8Z(8), a STRING not a number [standard]

PACKET = struct.Struct(_HEADER + _FIELDS)

FIELD_NAMES = [
    "time_ms",
    "position_x", "position_y", "position_z",
    "velocity_x", "velocity_y", "velocity_z",
    "PitchDegrees", "YawDegrees", "RollDegrees",
    "SpeedKmh", "altitude",
    "LocalSurgeMs2", "LocalSwayMs2", "LocalHeaveMs2",
    "rounds_fired", "fire_impulse",
    "ammo_primary", "ammo_max", "heat_primary",
    "is_overheated", "shoot_left",
    "missiles_fired", "missiles_available", "missile_warning",
    "health", "shield", "shield_max",
    "boost_axis", "boost_active", "boost_time_pct",
    "landing_gear", "is_landing",
    "EngineRpm", "EngineMaxRpm", "Throttle", "Brake", "Gear",
]

#: Fixed-width UTF-8, NUL-terminated. SimHub reports gear as text ("N", "R",
#: "3"), and AZOM's AB9 shift kick fires on gear-STRING transitions, so this
#: has to be a string field rather than the integer it looks like it wants.
STRING_FIELDS = {"Gear": 8}

INT_FIELDS = frozenset((
    "time_ms", "is_overheated", "shoot_left", "missile_warning",
    "boost_active", "landing_gear", "is_landing",
))

assert PACKET.size == EXPECTED_PACKET_LENGTH, (
    "layout mismatch: packed %d bytes, definition expects %d"
    % (PACKET.size, EXPECTED_PACKET_LENGTH))


UNREAL_TO_SIMHUB = """
    PitchDegrees   positive = nose DOWN   (Unreal is nose-UP: negate)
    YawDegrees     0 = north, clockwise   (no true north in game; pass through)
    RollDegrees    positive = tilt right  (agrees with Unreal)
    SpeedKmh       km/h                   (we derive m/s: x3.6)
    Local*Ms2      m/s^2, heave EXCLUDES gravity
"""


def motion_fields(pitch_deg, yaw_deg, roll_deg, speed_ms,
                  surge_ms2, sway_ms2, heave_ms2):
    """Map our Unreal-derived motion onto SimHub's standard field conventions.

    On gravity: we differentiate velocity kinematically, so heave carries no
    accelerometer +g reaction term -- in free fall it reads -9.81, which is
    exactly what "excludes gravity" asks for. No correction applied.
    """
    return {
        "PitchDegrees": -pitch_deg,
        "YawDegrees": yaw_deg % 360.0,
        "RollDegrees": roll_deg,
        "SpeedKmh": speed_ms * MS_TO_KMH,
        "LocalSurgeMs2": surge_ms2,
        "LocalSwayMs2": sway_ms2,
        "LocalHeaveMs2": heave_ms2,
    }


def register_definition(simdef_path):
    """Point SimHub at our definition, and keep pointing at it.

    Rewritten every startup on purpose: the link is just a path, so rewriting
    heals it if the app or the definition has moved since last run.
    """
    simdef_path = os.path.abspath(simdef_path)
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SimHub",
                        "ExternalSims", "Registrations")
    os.makedirs(base, exist_ok=True)
    link = os.path.join(base, DEFINITION_UNIQUE_ID + ".simlink")
    with open(link, "w", encoding="utf-8") as handle:
        handle.write(simdef_path)
    return link


class Sender:
    """Owns the header state that must stay coherent for a whole session.

    SimHub uses PhysicsDiscontinuityCounter to tell "the craft teleported" from
    "the craft accelerated impossibly hard". Without it, a map change or
    respawn reads as a colossal acceleration spike and the platform slams.
    """

    TELEPORT_THRESHOLD_M = 250.0

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, sock=None):
        self.host = host
        self.port = port
        self._sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._owns_socket = sock is None
        self.emitter_instance_id = random.getrandbits(64) or 1
        self.session_id = random.getrandbits(64) or 1
        self.packets_counter = 0
        self.discontinuity_counter = 0
        self._last_position = None

    def close(self):
        if self._owns_socket:
            self._sock.close()

    def new_session(self):
        self.session_id = random.getrandbits(64) or 1
        self.bump_discontinuity()
        self._last_position = None

    def bump_discontinuity(self):
        self.discontinuity_counter = (self.discontinuity_counter + 1) & 0xFFFFFFFF

    def note_position(self, position):
        """Flag teleports so SimHub resets instead of chasing a fake spike."""
        if position is None:
            return
        if self._last_position is not None:
            jump = math_dist(position, self._last_position)
            if jump > self.TELEPORT_THRESHOLD_M:
                self.bump_discontinuity()
        self._last_position = tuple(position)

    def build(self, fields, session_time_s, running=True, paused=False,
              user_in_control=True, spectator=False, replay=False):
        self.packets_counter += 1
        values = [
            GAME_SIGNATURE, TELEMETRY_SIGNATURE, LAYOUT_MAJOR, LAYOUT_MINOR,
            self.emitter_instance_id,
            0,                                  # PacketId: reserved, always 0
            self.packets_counter,
            1 if running else 0,
            1 if paused else 0,
            self.session_id,
            1 if replay else 0,
            1 if user_in_control else 0,
            0,                                  # IsAIInControl
            1 if spectator else 0,
            float(session_time_s),
            self.discontinuity_counter,
        ]
        for name in FIELD_NAMES:
            width = STRING_FIELDS.get(name)
            if width is not None:
                values.append(encode_utf8z(fields.get(name, ""), width))
                continue
            value = fields.get(name, 0)
            values.append(int(value) if name in INT_FIELDS else float(value))
        return PACKET.pack(*values)

    def send(self, *args, **kwargs):
        packet = self.build(*args, **kwargs)
        self._sock.sendto(packet, (self.host, self.port))
        return packet


def encode_utf8z(text, width):
    """Fixed-width UTF-8 with a guaranteed NUL terminator.

    Truncation reserves room for the terminator and then backs off to a
    character boundary: slicing encoded bytes alone would happily cut a
    multi-byte character in half and hand the reader an invalid sequence.
    Gear labels are ASCII today, but the field is a string and should behave
    like one.
    """
    raw = ("" if text is None else str(text)).encode("utf-8")[:width - 1]
    raw = raw.decode("utf-8", "ignore").encode("utf-8")     # back to a boundary
    return raw + b"\x00" * (width - len(raw))


def gear_label(index):
    """SimHub-style gear text. 0 is neutral, negative is reverse."""
    if index is None:
        return "N"
    index = int(index)
    if index < 0:
        return "R"
    if index == 0:
        return "N"
    return str(index)


def math_dist(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def parse_packet(data):
    """Unpack for tests and diagnostics."""
    if len(data) != EXPECTED_PACKET_LENGTH:
        raise ValueError("expected %d bytes, got %d"
                         % (EXPECTED_PACKET_LENGTH, len(data)))
    raw = PACKET.unpack(data)
    result = {
        "game_signature": raw[0], "telemetry_signature": raw[1],
        "packets_counter": raw[6], "is_session_running": raw[7],
        "is_session_paused": raw[8], "session_id": raw[9],
        "session_time": raw[14], "discontinuity": raw[15],
    }
    result.update(zip(FIELD_NAMES, raw[16:]))
    for name in STRING_FIELDS:
        value = result.get(name)
        if isinstance(value, bytes):
            result[name] = value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return result
