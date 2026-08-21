import socket, statistics, sys, threading, time
sys.path.insert(0, "src")
from grebels_telemetry import simdef
from grebels_telemetry.bridge import Bridge
from grebels_telemetry.config import Config

cfg = Config.load()
cfg.output_mode = "simdef"
cfg.simdef_host = "127.0.0.1"
cfg.simdef_port = 30778          # spare port: leave 30777 to SimHub
bridge = Bridge(cfg)
bridge.start()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 30778)); s.settimeout(8)

frames, t0 = [], time.perf_counter()
try:
    while time.perf_counter() - t0 < 18.0:
        d = s.recvfrom(2048)[0]
        if len(d) == simdef.EXPECTED_PACKET_LENGTH:
            frames.append(simdef.parse_packet(d))
except Exception as e:
    print("capture stopped:", e)
bridge.stop()

print("packets: %d  status: %s" % (len(frames), bridge.status.snapshot().get("state")))
if not frames:
    raise SystemExit("no packets")

g = lambda k: [f[k] for f in frames]
moving = [f for f in frames if f["SpeedKmh"] > 20]
print("moving frames: %d/%d" % (len(moving), len(frames)))
print("sig ok: %s   discontinuities: %d   session running: %d" % (
    frames[0]["telemetry_signature"] == simdef.TELEMETRY_SIGNATURE,
    frames[-1]["discontinuity"], frames[-1]["is_session_running"]))

def stat(k, unit=""):
    v = g(k)
    print("  %-16s min %9.2f  mean %9.2f  max %9.2f %s" % (
        k, min(v), statistics.mean(v), max(v), unit))

for k, u in [("SpeedKmh","km/h"),("altitude","m"),("PitchDegrees","deg"),
             ("RollDegrees","deg"),("LocalSurgeMs2","m/s2"),("LocalSwayMs2","m/s2"),
             ("LocalHeaveMs2","m/s2"),("EngineRpm","rpm"),("Throttle",""),
             ("health",""),("shield",""),("ammo_primary",""),("heat_primary","")]:
    stat(k, u)

print("gear: %r   ammo_max: %.0f   missiles: %.0f   overheated: %d" % (
    frames[-1]["Gear"], frames[-1]["ammo_max"],
    frames[-1]["missiles_available"], frames[-1]["is_overheated"]))
print("rounds fired during capture: %.0f   peak fire_impulse: %.2f" % (
    sum(g("rounds_fired")), max(g("fire_impulse"))))

# --- independent G check: differentiate speed over a 0.4 s window ---
if len(moving) > 50:
    t = [f["session_time"] for f in moving]
    sp = [f["SpeedKmh"] / 3.6 for f in moving]
    ref = []
    for i in range(len(moving)):
        j = i
        while j > 0 and t[i] - t[j] < 0.4:
            j -= 1
        dt = t[i] - t[j]
        ref.append((sp[i] - sp[j]) / dt if dt > 0.05 else 0.0)
    surge = [f["LocalSurgeMs2"] for f in moving]
    resid = statistics.pstdev([surge[i] - ref[i] for i in range(len(moving))])
    flips = sum(1 for i in range(1, len(surge)) if surge[i] * surge[i-1] < 0)
    print("G CHECK  surge sd %.2f  reference sd %.2f  residual sd %.2f  flips/s %.1f" % (
        statistics.pstdev(surge), statistics.pstdev(ref), resid,
        flips / max(1e-6, t[-1] - t[0])))
