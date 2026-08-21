import csv, statistics, sys
sys.path.insert(0, "src")
from grebels_telemetry.bridge import fit_motion

rows = list(csv.DictReader(open("trace_vel.csv")))
f = lambda r, k: float(r[k])
print("rows: %d  wall span %.1f s" % (len(rows), f(rows[-1], "wall") - f(rows[0], "wall")))

# The bridge only keeps a sample when the POSITION changes; replicate that.
uniq, last = [], None
for r in rows:
    p = (f(r, "px"), f(r, "py"), f(r, "pz"))
    if p != last:
        uniq.append((f(r, "gt"), (p[1] / 100.0, p[2] / 100.0, p[0] / 100.0)))  # -> packet axes, m
        last = p
print("distinct positions: %d" % len(uniq))

gts = [u[0] for u in uniq]
dts = [gts[i] - gts[i-1] for i in range(1, len(uniq))]
print("game-clock dt between position changes:")
print("  min %.6f  median %.6f  mean %.6f  max %.6f  <=0: %d" % (
    min(dts), statistics.median(dts), statistics.mean(dts), max(dts),
    sum(1 for d in dts if d <= 0)))
print("  update rate ~%.1f Hz" % (1.0 / statistics.median(dts)))
zero = sum(1 for d in dts if d == 0.0)
print("  position moved with NO clock advance: %d (%.1f%%)" % (
    zero, 100.0 * zero / len(dts)))

# speed straight from consecutive samples
sp = [((sum((uniq[i][1][k] - uniq[i-1][1][k]) ** 2 for k in range(3))) ** 0.5) / dts[i-1]
      for i in range(1, len(uniq)) if dts[i-1] > 0]
print("\nstep speed: median %.1f  p95 %.1f  max %.1f m/s" % (
    statistics.median(sp), sorted(sp)[int(len(sp) * 0.95)], max(sp)))

# acceleration for several fit windows
print("\nfit window comparison (surge magnitude along travel):")
for win in (0.10, 0.30, 0.60, 1.00):
    accs = []
    for i in range(len(uniq)):
        t_now = uniq[i][0]
        w = [(t, p) for t, p in uniq[max(0, i - 400):i + 1] if t_now - t <= win]
        if len(w) < 4:
            continue
        a = [fit_motion(w, ax)[1] for ax in range(3)]
        accs.append(sum(x * x for x in a) ** 0.5)
    if not accs:
        continue
    sat = sum(1 for a in accs if a >= 6 * 9.80665)
    print("  %.2fs: median %7.2f  p95 %8.2f  max %9.1f m/s2   >=6g: %5.1f%%" % (
        win, statistics.median(accs), sorted(accs)[int(len(accs) * 0.95)],
        max(accs), 100.0 * sat / len(accs)))
