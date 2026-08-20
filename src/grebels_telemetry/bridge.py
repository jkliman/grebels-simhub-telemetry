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

from . import calibrate, dr2
from .memory import Process, ProcessNotRunning

GAME_PROCESS = "G_Rebels-Win64-Shipping.exe"
GRAVITY = 9.80665
TARGET_FILENAME = "gr_target.txt"


# ------------------------------------------------------------ target file --
def read_target_file(path):
    """Parse the key=value snapshot published by the UE4SS mod."""
    data = {}
    with open(path) as handle:
        for line in handle:
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
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
        self._craft = target["pawn"].split(" ")[0]
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
            self._craft = target["pawn"].split(" ")[0]
            with self._lock:
                self._samples.clear()
                self.status.update(craft=self._craft)
        if target["world_addr"]:
            self._world = target["world_addr"]

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
                  dr2.unreal_to_packet_space(right_ue))
        with self._lock:
            self._samples.append(sample)
        return True

    # -- derive -------------------------------------------------------------
    def derive(self):
        """Latest kinematics, or None if we do not have enough history yet."""
        with self._lock:
            if not self._samples:
                return None
            samples = list(self._samples)

        game_time, position, nose, right = samples[-1]

        if time.perf_counter() - self._last_change_wall > self.config.stale_after_s:
            # Game paused or in a menu. Settle the platform rather than letting
            # the last velocity coast on forever.
            return dict(time=game_time, position=position, velocity=(0.0,) * 3,
                        acceleration=(0.0,) * 3, nose=nose, right=right,
                        speed=0.0, stale=True)

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
                    nose=nose, right=right,
                    speed=dr2.magnitude(velocity), stale=False)

    # -- sender -------------------------------------------------------------
    def _sender_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        destination = (self.config.host, self.config.port)
        self.status.update(destination="%s:%d" % destination)
        interval = 1.0 / self.config.send_rate_hz
        next_tick = time.perf_counter()
        distance = 0.0
        previous_time = None
        sent = 0
        sent_at_window_start = 0
        rate_start = time.perf_counter()

        try:
            while not self._stop.is_set():
                state = self.derive()
                if state is not None:
                    if state["stale"]:
                        self.status.update(state=Status.PAUSED, speed_ms=0.0,
                                           g_longitudinal=0.0, g_lateral=0.0)
                    else:
                        if self.status.state != Status.STREAMING:
                            self.status.update(state=Status.STREAMING, detail="")

                    if previous_time is not None and state["time"] > previous_time:
                        distance += state["speed"] * (state["time"] - previous_time)
                    previous_time = state["time"]

                    limit = self.config.g_clamp
                    if self.config.send_g_forces:
                        g_long = max(-limit, min(limit, dr2.dot(
                            state["acceleration"], state["nose"]) / GRAVITY))
                        g_lat = max(-limit, min(limit, dr2.dot(
                            state["acceleration"], state["right"]) / GRAVITY))
                    else:
                        g_long = g_lat = 0.0

                    sock.sendto(dr2.build_packet(
                        state["time"], state["position"], state["velocity"],
                        state["speed"], state["right"], state["nose"],
                        g_lat, g_long, distance=distance), destination)
                    sent += 1

                    self.status.update(
                        speed_ms=state["speed"], altitude_m=state["position"][1],
                        g_longitudinal=g_long, g_lateral=g_lat, packets_sent=sent)

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
