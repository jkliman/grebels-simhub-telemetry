import socket, statistics, sys, time
sys.path.insert(0, "src")
from grebels_telemetry import simdef
from grebels_telemetry.bridge import Bridge
from grebels_telemetry.config import Config

cfg = Config.load()
cfg.output_mode = "simdef"; cfg.simdef_host = "127.0.0.1"; cfg.simdef_port = 30778
b = Bridge(cfg); b.start()

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 30778)); s.settimeout(10)

print("waiting for speed...", flush=True)
frames, t0 = [], time.perf_counter()
armed = False
while time.perf_counter() - t0 < 75.0:
    try:
        d = s.recvfrom(2048)[0]
    except socket.timeout:
        break
    if len(d) != simdef.EXPECTED_PACKET_LENGTH:
        continue
    f = simdef.parse_packet(d)
    if not armed:
        if f["SpeedKmh"] > 250:
            armed = True; t1 = time.perf_counter()
            print("armed at %.0f km/h" % f["SpeedKmh"], flush=True)
        continue
    frames.append(f)
    if time.perf_counter() - t1 > 25.0:
        break
b.stop()

snap = b.status.snapshot()
print("packets: %d   clamped: %s   duplicate ticks: %s" % (
    len(frames), snap.get("clamped"), snap.get("duplicate_ticks")), flush=True)
if not frames:
    raise SystemExit("no fast frames captured")

g = lambda k: [f[k] for f in frames]
def stat(k, u=""):
    v = g(k)
    print("  %-16s min %8.2f  mean %8.2f  max %8.2f %s" % (k, min(v), statistics.mean(v), max(v), u))

for k, u in [("SpeedKmh","km/h"),("LocalSurgeMs2","m/s2"),("LocalSwayMs2","m/s2"),
             ("LocalHeaveMs2","m/s2"),("EngineRpm","rpm"),("heat_primary",""),
             ("ammo_primary",""),("ammo_max","")]:
    stat(k, u)

lim = 6 * 9.80665
sat = sum(1 for f in frames if abs(f["LocalSurgeMs2"]) >= lim - 0.01
          or abs(f["LocalSwayMs2"]) >= lim - 0.01 or abs(f["LocalHeaveMs2"]) >= lim - 0.01)
print("frames at the clamp: %d/%d (%.1f%%)" % (sat, len(frames), 100.0*sat/len(frames)))

# --- weapons ---
total_rounds = sum(g("rounds_fired"))
ammo = g("ammo_primary"); heat = g("heat_primary")
print("\nWEAPONS")
print("  rounds_fired total: %.0f   peak fire_impulse: %.2f" % (total_rounds, max(g("fire_impulse"))))
print("  ammo: start %.0f  min %.0f  end %.0f   ammo_max: %.0f" % (
    ammo[0], min(ammo), ammo[-1], frames[-1]["ammo_max"]))
print("  shoot_left toggled: %d times" % sum(
    1 for i in range(1, len(frames)) if frames[i]["shoot_left"] != frames[i-1]["shoot_left"]))
print("  overheated frames: %d" % sum(g("is_overheated")))

firing = [i for i in range(len(frames)) if frames[i]["rounds_fired"] > 0]
if firing and len(heat) > 20:
    lo, hi = min(firing), max(firing)
    before = statistics.mean(heat[max(0, lo-40):lo]) if lo > 5 else heat[0]
    during = statistics.mean(heat[lo:hi+1])
    print("  heat before firing %.3f -> during %.3f  => %s" % (
        before, during,
        "RISES with firing (inversion correct)" if during > before + 0.01
        else "does NOT rise (inversion is WRONG)" if during < before - 0.01
        else "unchanged (inconclusive)"))
else:
    print("  no shots detected in window")

# --- G sanity vs an independent reference ---
t = g("session_time"); sp = [x/3.6 for x in g("SpeedKmh")]
ref = []
for i in range(len(frames)):
    j = i
    while j > 0 and t[i]-t[j] < 0.4: j -= 1
    dt = t[i]-t[j]
    ref.append((sp[i]-sp[j])/dt if dt > 0.05 else 0.0)
surge = g("LocalSurgeMs2")
resid = statistics.pstdev([surge[i]-ref[i] for i in range(len(frames))])
print("\nG CHECK  surge sd %.2f  reference sd %.2f  residual sd %.2f" % (
    statistics.pstdev(surge), statistics.pstdev(ref), resid))
