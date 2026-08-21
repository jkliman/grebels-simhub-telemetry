"""The telemetry bridge: game memory in, SimHub UDP out.

Runs two threads. The sampler polls the game far faster than it updates and
records a sample only when the transform actually changes, timestamped with the
game's own simulation clock. The sender runs at a fixed rate, derives velocity
and acceleration from the sample history, and emits one UDP packet per tick.

Why the sim clock rather than wall clock: measured against a recorded trace, the
craft advances a near-constant distance per game update while the wall-clock
intervals between those updates scatter by +-40%, and the correlation between
distance and measured interval is 0.04 -- i.e. none. Dividing good distances by
bad intervals is what produces phantom G. Reading the game's own clock makes the
numerator and denominator come from the same place.
"""

import collections
import math
import socket
import struct
import threading
import time

from . import calibrate, dr2, simdef
from .memory import Process, ProcessNotRunning

GAME_PROCESS = "G_Rebels-Win64-Shipping.exe"
GRAVITY = 9.80665
TARGET_FILENAME = "gr_target.txt"
FIRE_IMPULSE_DECAY_S = 0.08
GAMEPLAY_POLL_HZ = 200.0

# Unreal property kind -> (struct code, byte width). BoolProperty is read as a
# single byte: UE stores non-bitfield bools one per byte, and every bool we ask
# for sits at its own offset in the dump, so a plain non-zero test is safe.
PROPERTY_KINDS = {
    "DoubleProperty": ("<d", 8),
    "FloatProperty": ("<f", 4),
    "IntProperty": ("<i", 4),
    "BoolProperty": ("<B", 1),
    "ByteProperty": ("<B", 1),
}


# ------------------------------------------------------------ target file --
def read_target_file(path):
    """Parse the key=value snapshot published by the UE4SS mod."""
    data = {}
    fields = {}
    with open(path) as handle:
        for line in handle:
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
            if key == "field":
                # "offset,Kind,Name" -- Name last because Blueprint names may
                # contain spaces ("Force F Primary Fire") and question marks.
                parts = value.split(",", 2)
                if len(parts) == 3 and parts[1] in PROPERTY_KINDS:
                    try:
                        fields[parts[2]] = (int(parts[0]), parts[1])
                    except ValueError:
                        pass
            else:
                data[key] = value

    def triple(name):
        raw = data.get(name)
        if not raw or raw == "nil":
            return None
        try:
            parts = [float(x) for x in raw.split(",")]
        except ValueError:
            return None
        return tuple(parts) if len(parts) == 3 else None

    def address(name):
        raw = data.get(name)
        if not raw:
            return None
        try:
            value = int(raw, 16)
        except ValueError:
            return None
        return value or None

    return {
        "pawn": data.get("pawn", ""),
        "pawn_addr": address("pawn_addr"),
        "root_addr": address("root_addr"),
        "world_addr": address("world_addr"),
        "time_seconds": float(data.get("time_seconds", -1.0)),
        "loc": triple("loc"),
        "rot": triple("rot"),
        "vel": triple("vel"),
        "fields": fields,
    }


# ------------------------------------------------------------------ status --
class Status:
    """Thread-safe snapshot of what the bridge is doing, for the UI."""

    WAITING_FOR_GAME = "waiting for game"
    WAITING_FOR_MOD = "waiting for mod"
    CALIBRATING = "calibrating"
    STREAMING = "streaming"
    PAUSED = "game paused"
    ERROR = "error"

    def __init__(self):
        self._lock = threading.Lock()
        self.state = self.WAITING_FOR_GAME
        self.detail = ""
        self.craft = ""
        self.speed_ms = 0.0
        self.altitude_m = 0.0
        self.g_longitudinal = 0.0
        self.g_lateral = 0.0
        self.packets_sent = 0
        self.packet_rate = 0.0
        self.game_update_hz = 0.0
        self.offsets = {}
        self.destination = ""
        self.fields_resolved = 0
        self.clamped = 0
        self.duplicate_ticks = 0
        self.rounds_fired_total = 0
        self.ammo = 0.0
        self.heat = 0.0
        self.output_mode = ""

    def update(self, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def snapshot(self):
        with self._lock:
            return dict(self.__dict__)


# ------------------------------------------------------------- derivatives --
def _solve_3x3(matrix, rhs):
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    m = [matrix[i][:] + [rhs[i]] for i in range(3)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda r: abs(m[r][column]))
        if abs(m[pivot][column]) < 1e-18:
            return None
        m[column], m[pivot] = m[pivot], m[column]
        for row in range(3):
            if row == column:
                continue
            factor = m[row][column] / m[column][column]
            for k in range(column, 4):
                m[row][k] -= factor * m[column][k]
    return [m[i][3] / m[i][i] for i in range(3)]


def fit_motion(samples, axis):
    """Least-squares quadratic through position, evaluated at the newest sample.

    Returns (velocity, acceleration) for one axis.

    Fitting one quadratic gives both derivatives from a single pass over the
    window, which is dramatically better than differencing twice. On a recorded
    25-second flight, chained differences over 0.1 s reported a mean
    acceleration of 10.9 G where the true figure was 2.1 G -- the "signal" was
    five parts noise to one part physics. The same data through a 0.3 s
    quadratic gives 2.14 G, within 3% of truth, and cuts sign-flip rate from 33
    per second to under 3.

    The fit is evaluated at the END of the window rather than its centre, which
    keeps cue latency low; a centred fit would be quieter still but would lag
    the platform by half a window.
    """
    count = len(samples)
    if count < 4:
        return 0.0, 0.0

    origin = samples[-1][0]
    powers = [0.0] * 5
    rhs = [0.0, 0.0, 0.0]
    for time_s, vector in samples:
        t = time_s - origin
        value = vector[axis]
        term = 1.0
        for k in range(5):
            powers[k] += term
            term *= t
        rhs[0] += value
        rhs[1] += value * t
        rhs[2] += value * t * t

    coefficients = _solve_3x3(
        [[powers[0], powers[1], powers[2]],
         [powers[1], powers[2], powers[3]],
         [powers[2], powers[3], powers[4]]], rhs)
    if coefficients is None:
        return 0.0, 0.0
    # p(t) = a0 + a1 t + a2 t^2, evaluated at t = 0 (the newest sample)
    return coefficients[1], 2.0 * coefficients[2]


class Bridge:
    def __init__(self, config, status=None):
        self.config = config
        self.status = status or Status()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._samples = collections.deque(maxlen=8000)   # (game_t, pos, nose, right)
        self._process = None
        self._offsets = None
        self._root = None
        self._world = None
        self._craft = ""
        self._last_raw_position = None
        self._last_change_wall = 0.0
        self._threads = []
        self._pawn = None
        self._fields = {}
        self._gameplay = {}
        self._gameplay_span = None      # (base_offset, length) for one bulk read
        self._next_gameplay_read = 0.0
        self._last_ammo = None
        self._last_missiles = None
        self._last_shoot_left = None
        self._rounds_pending = 0.0
        self._missiles_pending = 0.0
        self._last_shot_at = -99.0
        self._in_menu = False
        self._duplicate_ticks = 0
        self._clamped = 0
        self._ammo_seen_max = 0.0

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._sampler_loop, daemon=True, name="sampler"),
            threading.Thread(target=self._sender_loop, daemon=True, name="sender"),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        if self._process:
            self._process.close()
            self._process = None
        self.status.update(state=Status.WAITING_FOR_GAME, detail="stopped")

    @property
    def running(self):
        return any(t.is_alive() for t in self._threads)

    # -- attach -------------------------------------------------------------
    def _attach(self):
        """Find the game, read the mod's snapshot, calibrate. Returns True on success."""
        try:
            self._process = Process(GAME_PROCESS)
        except ProcessNotRunning:
            self.status.update(state=Status.WAITING_FOR_GAME,
                               detail="start G-Rebels and load into a flight")
            return False
        except Exception as exc:
            self.status.update(state=Status.ERROR, detail=str(exc))
            return False

        target_path = self.config.target_file
        try:
            target = read_target_file(target_path)
        except OSError:
            self.status.update(
                state=Status.WAITING_FOR_MOD,
                detail="no %s yet - load into a flight" % TARGET_FILENAME)
            return False

        if not target["root_addr"] or not target["loc"]:
            self.status.update(state=Status.WAITING_FOR_MOD,
                               detail="mod has not found your craft yet")
            return False

        self.status.update(state=Status.CALIBRATING,
                           detail="locating fields in memory")
        try:
            offsets = calibrate.calibrate(self._process, target)
        except calibrate.CalibrationError as exc:
            if self.config.allow_fallback_offsets:
                offsets = {"location": calibrate.FALLBACK_LOCATION,
                           "rotation": calibrate.FALLBACK_ROTATION,
                           "world_time": calibrate.FALLBACK_WORLD_TIME}
                self.status.update(detail="calibration fell back to known offsets (%s)" % exc)
            else:
                self.status.update(state=Status.ERROR, detail=str(exc))
                return False

        self._offsets = offsets
        self._root = target["root_addr"]
        self._world = target["world_addr"]
        self._pawn = target["pawn_addr"]
        self._craft = target["pawn"].split(" ")[0]
        self._set_fields(target["fields"])
        with self._lock:
            self._samples.clear()
        self.status.update(state=Status.STREAMING, craft=self._craft,
                           offsets=offsets, detail="")
        return True

    def _refresh_target(self):
        """Pick up a new craft after death, respawn or level change."""
        try:
            target = read_target_file(self.config.target_file)
        except OSError:
            return
        root = target["root_addr"]
        if root and root != self._root:
            self._root = root
            self._pawn = target["pawn_addr"]
            self._craft = target["pawn"].split(" ")[0]
            self._set_fields(target["fields"])
            self._last_ammo = None
            self._last_missiles = None
            self._last_shoot_left = None
            with self._lock:
                self._samples.clear()
                self.status.update(craft=self._craft)
        elif target["fields"] and not self._fields:
            self._set_fields(target["fields"])
        if target["world_addr"]:
            self._world = target["world_addr"]

    def _set_fields(self, fields):
        """Record resolved property offsets and plan one bulk read for them.

        The properties we want are scattered across roughly 5 KB of the pawn.
        Reading them individually would be 20-odd ReadProcessMemory calls per
        tick; reading the whole span once and slicing locally is a single call,
        and the values are then guaranteed to come from the same instant rather
        than smeared across the sampling window.
        """
        self._fields = dict(fields or {})
        self._in_menu = "MainMenu" in (self._craft or "")
        if not self._fields:
            self._gameplay_span = None
            return
        lowest = min(offset for offset, _ in self._fields.values())
        highest = max(offset + PROPERTY_KINDS[kind][1]
                      for offset, kind in self._fields.values())
        self._gameplay_span = (lowest, highest - lowest)
        self.status.update(fields_resolved=len(self._fields))

    def _read_gameplay(self, now):
        """Sample weapons/shields/boost, and turn ammo changes into shot events.

        Shots are counted from the magazine going DOWN, never up: a reload
        raises the count and must not read as firing. Counting differences also
        means a burst faster than our poll rate still lands -- we see the size
        of the drop, not just that one happened.
        """
        if not self._fields or not self._pawn or self._gameplay_span is None:
            return
        if now < self._next_gameplay_read:
            return
        self._next_gameplay_read = now + 1.0 / GAMEPLAY_POLL_HZ

        base, length = self._gameplay_span
        block = self._process.read(self._pawn + base, length)
        if block is None:
            return

        values = {}
        for name, (offset, kind) in self._fields.items():
            code, width = PROPERTY_KINDS[kind]
            start = offset - base
            try:
                values[name] = struct.unpack_from(code, block, start)[0]
            except struct.error:
                continue

        # Shots are counted from the ALTERNATING BARREL flag, not from ammo.
        #
        # Measured over two live firing runs: PrimaryFireMagazineStatus reads 0
        # throughout flight while the guns are plainly firing -- ShootLeft
        # toggled 18 times and 816 frames came back overheated. The primary
        # weapon is heat-limited, not magazine-limited; what the pilot
        # experiences as "magazine emptied" is the overheat cutout. So the
        # magazine counter is not a shot source, and TotalHeatPrimary is the
        # real "can I shoot" resource.
        #
        # ShootLeft flips once per shot as the barrels alternate, which makes
        # it an exact per-shot event. It is gated on the fire controls so a
        # flip while not shooting cannot invent a round.
        shoot_left = values.get("ShootLeft")
        firing = bool(values.get("PrimaryFire_pressed")
                      or values.get("PrimaryFire_Success"))
        if shoot_left is not None:
            if (self._last_shoot_left is not None
                    and shoot_left != self._last_shoot_left and firing):
                self._rounds_pending += 1.0
                self._last_shot_at = now
            self._last_shoot_left = shoot_left

        ammo = values.get("PrimaryFireMagazineStatus")
        if ammo is not None:
            if ammo > self._ammo_seen_max:
                self._ammo_seen_max = float(ammo)
            self._last_ammo = ammo

        missiles = values.get("AvailableMissiles")
        if missiles is not None:
            if self._last_missiles is not None and missiles < self._last_missiles:
                self._missiles_pending += self._last_missiles - missiles
            self._last_missiles = missiles

        with self._lock:
            self._gameplay = values

    def _take_weapon_events(self, now):
        """Consume pending shot counts and shape them into a decaying impulse."""
        with self._lock:
            rounds = self._rounds_pending
            missiles = self._missiles_pending
            self._rounds_pending = 0.0
            self._missiles_pending = 0.0
            gameplay = dict(self._gameplay)
        elapsed = now - self._last_shot_at
        impulse = max(0.0, 1.0 - elapsed / FIRE_IMPULSE_DECAY_S)
        return rounds, missiles, impulse, gameplay

    # -- sampler ------------------------------------------------------------
    def _sampler_loop(self):
        poll_interval = 1.0 / self.config.poll_hz
        next_refresh = 0.0
        next_tick = time.perf_counter()
        updates = 0
        rate_window_start = time.perf_counter()

        while not self._stop.is_set():
            now = time.perf_counter()

            if self._process is None or self._offsets is None:
                if not self._attach():
                    self._stop.wait(1.0)
                    continue

            if now >= next_refresh:
                if not self._process.is_alive():
                    self._process.close()
                    self._process = None
                    self._offsets = None
                    self.status.update(state=Status.WAITING_FOR_GAME,
                                       detail="game closed")
                    continue
                self._refresh_target()
                next_refresh = now + self.config.target_refresh_s

            if self._read_once(now):
                updates += 1
            self._read_gameplay(now)

            if now - rate_window_start >= 1.0:
                self.status.update(game_update_hz=updates / (now - rate_window_start))
                updates = 0
                rate_window_start = now

            next_tick += poll_interval
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.perf_counter()

    def _read_once(self, now):
        """One sandwiched read. Returns True if a new game update was captured."""
        offsets = self._offsets
        clock_addr = self._world + offsets["world_time"] if self._world else None

        before = self._process.read(clock_addr, 8) if clock_addr else None
        position_raw = self._process.read(self._root + offsets["location"], 24)
        rotation_raw = self._process.read(self._root + offsets["rotation"], 24)
        after = self._process.read(clock_addr, 8) if clock_addr else None

        if position_raw is None or rotation_raw is None:
            self._offsets = None          # craft went away; re-attach
            return False
        if clock_addr is not None and (before is None or before != after):
            return False                  # spanned a frame boundary, discard

        game_time = struct.unpack("<d", before)[0] if before else now
        x, y, z = struct.unpack("<3d", position_raw)
        pitch, yaw, roll = struct.unpack("<3d", rotation_raw)

        values = (x, y, z, pitch, yaw, roll, game_time)
        if any(math.isnan(v) or math.isinf(v) or abs(v) > 1e12 for v in values):
            return False
        if (x, y, z) == self._last_raw_position:
            return False

        self._last_raw_position = (x, y, z)
        self._last_change_wall = now

        forward_ue, right_ue = dr2.unreal_rotator_to_axes(pitch, yaw, roll)
        sample = (game_time,
                  (y / 100.0, z / 100.0, x / 100.0),        # cm -> m, packet axes
                  dr2.unreal_to_packet_space(forward_ue),
                  dr2.unreal_to_packet_space(right_ue),
                  (pitch, yaw, roll))                       # raw, for SimHub native
        with self._lock:
            if self._samples and self._samples[-1][0] == game_time:
                # The craft moved but the sim clock did not tick. Measured at
                # 22% of updates on a 25 s trace. Appending both would hand the
                # fit two different positions at the SAME instant, which is a
                # division by zero wearing a disguise -- it produced spikes up
                # to 1661 m/s2 (169 g) and pinned the output at the clamp.
                # Keep the newest position for the tick instead of stacking.
                self._samples[-1] = sample
                self._duplicate_ticks += 1
                return False
            self._samples.append(sample)
        return True

    # -- derive -------------------------------------------------------------
    def derive(self):
        """Latest kinematics, or None if we do not have enough history yet."""
        with self._lock:
            if not self._samples:
                return None
            samples = list(self._samples)

        game_time, position, nose, right, rotation = samples[-1]

        if time.perf_counter() - self._last_change_wall > self.config.stale_after_s:
            # Game paused or in a menu. Settle the platform rather than letting
            # the last velocity coast on forever.
            return dict(time=game_time, position=position, velocity=(0.0,) * 3,
                        acceleration=(0.0,) * 3, nose=nose, right=right,
                        rotation=rotation, speed=0.0, stale=True)

        window = [(s[0], s[1]) for s in samples
                  if game_time - s[0] <= self.config.fit_window_s]
        if len(window) < 4:
            return None

        velocity = []
        acceleration = []
        for axis in range(3):
            v, a = fit_motion(window, axis)
            velocity.append(v)
            acceleration.append(a)

        return dict(time=game_time, position=position,
                    velocity=tuple(velocity), acceleration=tuple(acceleration),
                    nose=nose, right=right, rotation=rotation,
                    speed=dr2.magnitude(velocity), stale=False)

    # -- sender -------------------------------------------------------------
    def _gameplay_fields(self, gameplay, rounds, missiles, impulse):
        """Gameplay properties mapped onto the .simdef field names."""
        def value(name, default=0.0):
            got = gameplay.get(name)
            return default if got is None else got

        # Measured at rest: TotalHeatPrimary == MaxHeatPrimary == 100, so
        # "Total" is remaining capacity, not accumulated heat -- full when
        # cold. Inverted here so the channel rises as the gun heats up, which
        # is what an effect wants. Confirm the direction while firing.
        heat_max = value("MaxHeatPrimary")
        heat = 1.0 - (value("TotalHeatPrimary") / heat_max) if heat_max else 0.0

        return {
            "rounds_fired": rounds,
            "fire_impulse": impulse,
            "ammo_primary": value("PrimaryFireMagazineStatus"),
            "ammo_max": max(self._ammo_seen_max,
                            value("PrimaryFireMagazineSize")),
            "heat_primary": max(0.0, min(1.0, heat)),
            "is_overheated": 1 if value("isOverheatedPrimary") else 0,
            "shoot_left": 1 if value("ShootLeft") else 0,
            "missiles_fired": missiles,
            "missiles_available": value("AvailableMissiles"),
            "missile_warning": 1 if value("MissileNotificationActive") else 0,
            "health": value("Health"),
            "shield": value("ShieldHealthCurrent"),
            "shield_max": value("ShieldHealthMax"),
            "boost_axis": value("BoostAxis"),
            "boost_active": 1 if value("EngineBoosterIsActive") else 0,
            "boost_time_pct": value("EngineBoostTimePercentage"),
            "landing_gear": 1 if value("LandingGearActive") else 0,
            "is_landing": 1 if value("isLanding") else 0,
        }

    def _engine_fields(self, gameplay, speed_ms, surge_ms2):
        """Fabricate engine channels so car-shaped plugins have something to bite on.

        G-Rebels has no engine and no gearbox. AZOM drives the AB9's vibration
        frequency from rpm/maxRpm and fires its shift kick on gear-string
        changes, so without these the stick stays silent. RPM leans mostly on
        speed with a boost contribution, which makes the buzz rise as you
        accelerate and jump when the booster lights.
        """
        config = self.config
        if not config.synth_engine:
            return {"Gear": "N"}

        idle, top = config.synth_idle_rpm, config.synth_max_rpm

        # Prefer the game's own maximum; it is in Unreal units (cm/s) and is
        # occasionally zero or absurd, so it is range-checked before use.
        reference = config.synth_reference_speed_ms
        game_max = gameplay.get("CurrentMaxVelocity")
        if game_max:
            candidate = game_max / 100.0
            if 20.0 <= candidate <= 1000.0:
                reference = candidate

        speed_fraction = max(0.0, min(1.0, speed_ms / reference if reference else 0.0))

        boost_axis = gameplay.get("BoostAxis") or 0.0
        boosting = bool(gameplay.get("EngineBoosterIsActive"))
        boost = 1.0 if boosting else max(0.0, min(1.0, boost_axis))

        load = 0.72 * speed_fraction + 0.28 * boost
        rpm = idle + load * (top - idle)

        # Deceleration reads as braking. Half a g of retardation is treated as
        # full brake, which is roughly where airbraking becomes obvious.
        brake = max(0.0, min(1.0, -surge_ms2 / (0.5 * GRAVITY)))

        if config.synth_gear:
            count = max(1, int(config.synth_gear_count))
            gear = simdef.gear_label(min(count, int(speed_fraction * count) + 1))
        else:
            gear = "N"

        return {
            "EngineRpm": rpm,
            "EngineMaxRpm": top,
            "Throttle": 1.0 if boosting else max(0.0, min(1.0, boost_axis)),
            "Brake": brake,
            "Gear": gear,
        }

    def _sender_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        mode = self.config.output_mode
        send_dr2 = mode in ("dr2", "both")
        send_native = mode in ("simdef", "both")

        dr2_destination = (self.config.host, self.config.port)
        native = None
        destinations = []
        if send_dr2:
            destinations.append("DR2 %s:%d" % dr2_destination)
        if send_native:
            native = simdef.Sender(self.config.simdef_host,
                                   self.config.simdef_port, sock=sock)
            destinations.append("SimHub %s:%d" % (native.host, native.port))
        self.status.update(destination="  |  ".join(destinations),
                           output_mode=mode)

        interval = 1.0 / self.config.send_rate_hz
        next_tick = time.perf_counter()
        distance = 0.0
        previous_time = None
        sent = 0
        rounds_total = 0
        sent_at_window_start = 0
        rate_start = time.perf_counter()

        try:
            while not self._stop.is_set():
                state = self.derive()
                if state is not None:
                    wall = time.perf_counter()
                    rounds, missiles, impulse, gameplay = \
                        self._take_weapon_events(wall)
                    rounds_total += int(rounds)

                    if state["stale"]:
                        self.status.update(state=Status.PAUSED, speed_ms=0.0,
                                           g_longitudinal=0.0, g_lateral=0.0)
                    elif self.status.state != Status.STREAMING:
                        self.status.update(state=Status.STREAMING, detail="")

                    if previous_time is not None and state["time"] > previous_time:
                        distance += state["speed"] * (state["time"] - previous_time)
                    previous_time = state["time"]

                    # Body-frame acceleration. "up" completes the right-handed
                    # frame: in packet space X=right, Y=up, Z=forward, so
                    # nose x right = up.
                    nose, right = state["nose"], state["right"]
                    up = dr2.cross(nose, right)
                    acceleration = state["acceleration"]
                    surge = dr2.dot(acceleration, nose)
                    sway = dr2.dot(acceleration, right)
                    heave = dr2.dot(acceleration, up)

                    limit = self.config.g_clamp
                    if self.config.send_g_forces:
                        clamp = lambda v: max(-limit, min(limit, v / GRAVITY))
                        g_long, g_lat = clamp(surge), clamp(sway)
                    else:
                        g_long = g_lat = 0.0

                    if send_dr2:
                        sock.sendto(dr2.build_packet(
                            state["time"], state["position"], state["velocity"],
                            state["speed"], right, nose,
                            g_lat, g_long, distance=distance), dr2_destination)

                    if send_native:
                        pitch, yaw, roll = state["rotation"]
                        limit_ms2 = limit * GRAVITY

                        def clamp_ms2(v):
                            if v > limit_ms2 or v < -limit_ms2:
                                self._clamped += 1
                                return max(-limit_ms2, min(limit_ms2, v))
                            return v
                        fields = simdef.motion_fields(
                            pitch, yaw, roll, state["speed"],
                            clamp_ms2(surge), clamp_ms2(sway), clamp_ms2(heave))
                        position, velocity = state["position"], state["velocity"]
                        fields.update({
                            # int32 ms wraps after 24.8 days of game clock
                            "time_ms": int(state["time"] * 1000.0) & 0x7FFFFFFF,
                            "position_x": position[0],
                            "position_y": position[1],
                            "position_z": position[2],
                            "velocity_x": velocity[0],
                            "velocity_y": velocity[1],
                            "velocity_z": velocity[2],
                            "altitude": position[1],
                        })
                        fields.update(self._gameplay_fields(
                            gameplay, rounds, missiles, impulse))
                        fields.update(self._engine_fields(
                            gameplay, state["speed"], surge))
                        native.note_position(position)
                        native.send(fields, state["time"],
                                    running=not self._in_menu,
                                    paused=bool(state["stale"]))

                    sent += 1
                    self.status.update(
                        speed_ms=state["speed"], altitude_m=state["position"][1],
                        g_longitudinal=g_long, g_lateral=g_lat, packets_sent=sent,
                        rounds_fired_total=rounds_total,
                        clamped=self._clamped,
                        duplicate_ticks=self._duplicate_ticks,
                        ammo=gameplay.get("PrimaryFireMagazineStatus", 0.0) or 0.0,
                        heat=self._gameplay_fields(
                            gameplay, 0, 0, 0)["heat_primary"])

                now = time.perf_counter()
                if now - rate_start >= 1.0:
                    self.status.update(
                        packet_rate=(sent - sent_at_window_start) / (now - rate_start))
                    sent_at_window_start = sent
                    rate_start = now

                next_tick += interval
                sleep_for = next_tick - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.perf_counter()
        finally:
            sock.close()
