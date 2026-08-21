import struct, sys, time
sys.path.insert(0, "src")
from grebels_telemetry.bridge import read_target_file, GAME_PROCESS
from grebels_telemetry.config import Config
from grebels_telemetry.memory import Process

cfg = Config.load(); proc = Process(GAME_PROCESS)

def wait_moving(timeout=120.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        t = read_target_file(cfg.target_file)
        v = t["vel"]
        if v and max(abs(x) for x in v) > 2000.0:      # > 20 m/s
            return t
        time.sleep(0.15)
    return None

print("waiting for fast flight...", flush=True)
t = wait_moving()
if not t: raise SystemExit("timed out")
vel = t["vel"]
print("vel:", vel, flush=True)

# Scan BOTH the pawn and the root component, far wider than before.
cands = []
for label, base in (("pawn", t["pawn_addr"]), ("root", t["root_addr"])):
    blob = proc.read(base, 16384)
    if not blob:
        print(label, "unreadable"); continue
    scale = max(1.0, max(abs(v) for v in vel))
    for off in range(0, len(blob) - 24, 4):
        try:
            a, b, c = struct.unpack_from("<3d", blob, off)
        except struct.error:
            break
        if abs(a) > 1e9 or abs(b) > 1e9 or abs(c) > 1e9:
            continue
        if max(abs(a-vel[0]), abs(b-vel[1]), abs(c-vel[2])) / scale < 0.03:
            cands.append((label, base, off))
    # floats too, in case velocity is single precision
    for off in range(0, len(blob) - 12, 4):
        a, b, c = struct.unpack_from("<3f", blob, off)
        if abs(a) > 1e9 or abs(b) > 1e9 or abs(c) > 1e9:
            continue
        if max(abs(a-vel[0]), abs(b-vel[1]), abs(c-vel[2])) / scale < 0.03:
            cands.append((label + "/f32", base, off))
print("candidates:", [(c[0], c[2]) for c in cands][:12], flush=True)

# Confirm across 4 s of flight: a real velocity field tracks every update.
for label, base, off in cands[:6]:
    isf = label.endswith("/f32")
    ok = miss = 0
    for _ in range(40):
        t2 = read_target_file(cfg.target_file)
        pub = t2["vel"]
        b2 = t2["pawn_addr"] if label.startswith("pawn") else t2["root_addr"]
        raw = proc.read(b2 + off, 12 if isf else 24)
        if raw and pub and max(abs(x) for x in pub) > 100:
            got = struct.unpack("<3f" if isf else "<3d", raw)
            sc = max(1.0, max(abs(v) for v in pub))
            if max(abs(got[i]-pub[i]) for i in range(3)) / sc < 0.2: ok += 1
            else: miss += 1
        time.sleep(0.1)
    print("  %-10s off %5d -> matched %d / missed %d" % (label, off, ok, miss), flush=True)
proc.close()
