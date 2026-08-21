import struct, sys, time
sys.path.insert(0, "src")
from grebels_telemetry.bridge import read_target_file, GAME_PROCESS
from grebels_telemetry.config import Config
from grebels_telemetry.memory import Process

cfg = Config.load()
proc = Process(GAME_PROCESS)
t = read_target_file(cfg.target_file)
root, world = t["root_addr"], t["world_addr"]
vel = t["vel"]
print("published vel:", vel)
if not vel or vel == (0.0, 0.0, 0.0):
    raise SystemExit("craft not moving -- fly first")

# Scan the root component for three consecutive doubles matching the mod's
# published velocity. Location sits at 328, so search a generous window.
blob = proc.read(root, 2048)
hits = []
for off in range(0, 2048 - 24, 8):
    a, b, c = struct.unpack_from("<3d", blob, off)
    if all(abs(x) < 1e9 for x in (a, b, c)):
        err = max(abs(a - vel[0]), abs(b - vel[1]), abs(c - vel[2]))
        scale = max(1.0, max(abs(v) for v in vel))
        if err / scale < 0.02:
            hits.append((off, (a, b, c), err))
print("velocity candidates:", [(h[0], round(h[2], 3)) for h in hits])

if hits:
    voff = hits[0][0]
    print("\nconfirming offset %d over 3 s of flight..." % voff)
    ok = miss = 0
    for _ in range(30):
        t2 = read_target_file(cfg.target_file)
        pub = t2["vel"]
        raw = proc.read(t2["root_addr"] + voff, 24)
        if raw and pub:
            got = struct.unpack("<3d", raw)
            scale = max(1.0, max(abs(v) for v in pub))
            if max(abs(got[i] - pub[i]) for i in range(3)) / scale < 0.15:
                ok += 1
            else:
                miss += 1
        time.sleep(0.1)
    print("matched %d, missed %d" % (ok, miss))
proc.close()
