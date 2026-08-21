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
s.bind(("127.0.0.1", 30778)); s.settimeout(12)

frames, t0 = [], time.perf_counter()
print("RECORDING 30s - SHOOT NOW", flush=True)
try:
    while time.perf_counter() - t0 < 30.0:
        d = s.recvfrom(2048)[0]
        if len(d) == simdef.EXPECTED_PACKET_LENGTH:
            frames.append(simdef.parse_packet(d))
except socket.timeout:
    pass
b.stop()
print("packets: %d" % len(frames), flush=True)
if not frames: raise SystemExit("nothing captured")

g = lambda k: [f[k] for f in frames]
ammo, heat, rounds = g("ammo_primary"), g("heat_primary"), g("rounds_fired")
print("\nAMMO   start %.0f  min %.0f  max %.0f  end %.0f   ammo_max %.0f" % (
    ammo[0], min(ammo), max(ammo), ammo[-1], frames[-1]["ammo_max"]))
print("ROUNDS total %.0f   frames with a shot %d   peak impulse %.2f" % (
    sum(rounds), sum(1 for r in rounds if r > 0), max(g("fire_impulse"))))
print("SHOOT_LEFT toggles %d   overheated frames %d/%d" % (
    sum(1 for i in range(1, len(frames)) if frames[i]["shoot_left"] != frames[i-1]["shoot_left"]),
    sum(g("is_overheated")), len(frames)))
print("HEAT   min %.3f  mean %.3f  max %.3f" % (min(heat), statistics.mean(heat), max(heat)))

fire_idx = [i for i, r in enumerate(rounds) if r > 0]
if fire_idx:
    lo, hi = fire_idx[0], fire_idx[-1]
    pre = statistics.mean(heat[max(0, lo-50):lo]) if lo > 10 else heat[0]
    dur = statistics.mean(heat[lo:hi+1])
    print("HEAT   %.3f before first shot -> %.3f while firing  => %s" % (
        pre, dur, "RISES (inversion CORRECT)" if dur > pre + 0.02
        else "FALLS (inversion WRONG)" if dur < pre - 0.02 else "flat (inconclusive)"))
    # does the impulse decay as designed?
    peak = max(range(len(frames)), key=lambda i: frames[i]["fire_impulse"])
    tail = [round(frames[i]["fire_impulse"], 2) for i in range(peak, min(peak+10, len(frames)))]
    print("IMPULSE decay from peak: %s" % tail)
    print("AMMO   dropped %.0f over the run; rounds_fired counted %.0f" % (
        max(ammo) - min(ammo), sum(rounds)))
else:
    print("NO SHOTS SEEN - ammo never decreased")

sp = g("SpeedKmh")
print("\nSPEED  mean %.0f  max %.0f km/h" % (statistics.mean(sp), max(sp)))
lim = 6*9.80665
sat = sum(1 for f in frames if abs(f["LocalSurgeMs2"]) >= lim-0.01)
print("surge at clamp: %.1f%%   mean surge %.2f m/s2" % (
    100.0*sat/len(frames), statistics.mean(g("LocalSurgeMs2"))))
