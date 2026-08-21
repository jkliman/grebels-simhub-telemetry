import statistics, struct, sys, time
sys.path.insert(0, "src")
from grebels_telemetry import calibrate
from grebels_telemetry.bridge import Bridge, read_target_file, GAME_PROCESS
from grebels_telemetry.config import Config
from grebels_telemetry.memory import Process

cfg = Config.load()
target = read_target_file(cfg.target_file)
proc = Process(GAME_PROCESS)

try:
    offs = calibrate.calibrate(proc, target)
    print("calibration OK:", offs)
except Exception as e:
    offs = {"location": calibrate.FALLBACK_LOCATION,
            "rotation": calibrate.FALLBACK_ROTATION,
            "world_time": calibrate.FALLBACK_WORLD_TIME}
    print("calibration FAILED ->", e)
    print("using fallbacks:", offs)

root, world, pawn = target["root_addr"], target["world_addr"], target["pawn_addr"]

# --- is the world clock sane? sample it raw at 1 kHz ---
samples = []
t0 = time.perf_counter()
while time.perf_counter() - t0 < 4.0:
    raw = proc.read(world + offs["world_time"], 8)
    pos = proc.read(root + offs["location"], 24)
    if raw and pos:
        samples.append((time.perf_counter() - t0,
                        struct.unpack("<d", raw)[0],
                        struct.unpack("<3d", pos)))
    time.sleep(0.001)

print("\nraw samples: %d over %.1f s" % (len(samples), samples[-1][0]))
gt = [s[1] for s in samples]
print("game clock: first %.4f  last %.4f  span %.4f s" % (gt[0], gt[-1], gt[-1] - gt[0]))
changes = [(samples[i][0], gt[i] - gt[i-1]) for i in range(1, len(samples)) if gt[i] != gt[i-1]]
print("distinct clock ticks: %d" % len(changes))
if changes:
    d = [c[1] for c in changes]
    print("  clock dt: min %.6f  median %.6f  max %.6f   negative: %d" % (
        min(d), statistics.median(d), max(d), sum(1 for x in d if x <= 0)))
    print("  implied update rate: %.1f Hz" % (1.0 / statistics.median(d)))

# how often does position change vs clock?
pmoves = sum(1 for i in range(1, len(samples)) if samples[i][2] != samples[i-1][2])
print("position changed %d times (%.1f Hz)" % (pmoves, pmoves / samples[-1][0]))

# distance per clock tick -- should be ~constant at steady speed
steps = []
for i in range(1, len(samples)):
    if gt[i] != gt[i-1]:
        a, b = samples[i-1][2], samples[i][2]
        dist = sum((b[k] - a[k]) ** 2 for k in range(3)) ** 0.5 / 100.0
        steps.append((dist, gt[i] - gt[i-1]))
if steps:
    sp = [d / t for d, t in steps if t > 0]
    print("speed from raw pos/clock: min %.1f median %.1f max %.1f m/s" % (
        min(sp), statistics.median(sp), max(sp)))

# --- the suspect gameplay properties, read raw ---
print("\nraw property values:")
kinds = {"DoubleProperty": ("<d", 8), "FloatProperty": ("<f", 4),
         "IntProperty": ("<i", 4), "BoolProperty": ("<B", 1)}
for name in ["PrimaryFireMagazineSize", "PrimaryFireMagazineStatus",
             "TotalHeatPrimary", "MaxHeatPrimary", "Health", "ShieldHealthMax",
             "CurrentVelocity", "CurrentMaxVelocity", "BoostAxis",
             "Force F Primary Fire"]:
    entry = target["fields"].get(name)
    if not entry:
        print("  %-26s NOT RESOLVED" % name); continue
    off, kind = entry
    code, width = kinds[kind]
    raw = proc.read(pawn + off, width)
    val = struct.unpack(code, raw)[0] if raw else None
    print("  %-26s %-16s = %s" % (name, kind, val))
proc.close()
