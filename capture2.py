import csv, struct, sys, time
sys.path.insert(0, "src")
from grebels_telemetry.bridge import read_target_file, GAME_PROCESS
from grebels_telemetry.config import Config
from grebels_telemetry.memory import Process

cfg = Config.load()
proc = Process(GAME_PROCESS)

def moving_target(timeout=180.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        t = read_target_file(cfg.target_file)
        v = t["vel"]
        if v and max(abs(x) for x in v) > 500.0:      # > 5 m/s
            return t
        time.sleep(0.2)
    return None

print("waiting for movement...", flush=True)
t = moving_target()
if not t:
    raise SystemExit("timed out waiting for flight")
print("moving:", t["vel"], flush=True)

# locate ComponentVelocity by matching the mod's published value
blob = proc.read(t["root_addr"], 2048)
vel, voff = t["vel"], None
for off in range(0, 2048 - 24, 8):
    a, b, c = struct.unpack_from("<3d", blob, off)
    scale = max(1.0, max(abs(v) for v in vel))
    if max(abs(a - vel[0]), abs(b - vel[1]), abs(c - vel[2])) / scale < 0.02:
        voff = off
        break
print("velocity offset:", voff, flush=True)

print("recording 25 s", flush=True)
rows = []
root, world = t["root_addr"], t["world_addr"]
t0 = time.perf_counter()
torn = 0
while time.perf_counter() - t0 < 25.0:
    before = proc.read(world + 384, 8)
    pos = proc.read(root + 328, 24)
    velraw = proc.read(root + voff, 24) if voff is not None else None
    after = proc.read(world + 384, 8)
    if before and pos and before == after:
        gt = struct.unpack("<d", before)[0]
        p = struct.unpack("<3d", pos)
        v = struct.unpack("<3d", velraw) if velraw else (0.0, 0.0, 0.0)
        rows.append((time.perf_counter() - t0, gt) + p + v)
    elif before != after:
        torn += 1
    time.sleep(0.001)

with open("trace_vel.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow("wall gt px py pz vx vy vz".split())
    w.writerows(rows)
print("recorded %d rows, %d torn reads" % (len(rows), torn), flush=True)
proc.close()
