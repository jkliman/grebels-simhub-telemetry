"""Offline checks that do not need Windows or a running game."""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from grebels_telemetry import dr2
from grebels_telemetry.bridge import fit_motion

failures = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        failures.append(label)


print("packet")
packet = dr2.build_packet(1.5, (10, 20, 30), (1, 2, 3), 4.0, (1, 0, 0), (0, 0, 1), 0.3, -0.7)
check("264 bytes", len(packet) == dr2.PACKET_SIZE, len(packet))
parsed = dr2.parse_packet(packet)
check("round trip", parsed["position"] == (10, 20, 30) and parsed["speed"] == 4.0)
check("g fields", abs(parsed["g_lateral"] - 0.3) < 1e-6
      and abs(parsed["g_longitudinal"] + 0.7) < 1e-6)

print("orientation")
for pitch, yaw, roll in [(0, 0, 0), (30, 0, 0), (0, 90, 0), (0, 0, 90), (12, -40, 25)]:
    forward, right = dr2.unreal_rotator_to_axes(pitch, yaw, roll)
    orthogonal = abs(dr2.dot(forward, right)) < 1e-9
    unit = abs(dr2.magnitude(forward) - 1) < 1e-9 and abs(dr2.magnitude(right) - 1) < 1e-9
    check("orthonormal at (%s,%s,%s)" % (pitch, yaw, roll), orthogonal and unit)
forward, _ = dr2.unreal_rotator_to_axes(30, 0, 0)
check("nose up is +Z in unreal space", forward[2] > 0.49)
check("axis remap", dr2.unreal_to_packet_space((1, 2, 3)) == (2, 3, 1))

print("motion fit")
# constant acceleration: x(t) = 0.5*a*t^2 + v0*t
accel, v0, dt = 7.5, 20.0, 0.008
samples = [(i * dt, (0.5 * accel * (i * dt) ** 2 + v0 * (i * dt), 0.0, 0.0))
           for i in range(40)]
velocity, acceleration = fit_motion(samples, 0)
end_t = samples[-1][0]
check("acceleration recovered", abs(acceleration - accel) < 1e-6, acceleration)
check("velocity recovered", abs(velocity - (v0 + accel * end_t)) < 1e-6, velocity)

# uneven sample spacing, as the game actually delivers
uneven = []
t = 0.0
for i in range(40):
    t += dt * (0.6 if i % 3 else 1.7)
    uneven.append((t, (0.5 * accel * t * t + v0 * t, 0.0, 0.0)))
_, acceleration = fit_motion(uneven, 0)
check("uneven spacing", abs(acceleration - accel) < 1e-6, acceleration)

# noise rejection: the estimator must not amplify jitter into phantom G
import random
random.seed(7)
noisy = [(t, (p[0] + random.uniform(-0.02, 0.02), 0.0, 0.0)) for t, p in samples]
_, noisy_accel = fit_motion(noisy, 0)
check("2 cm jitter stays under 3 G of error", abs(noisy_accel - accel) < 3 * 9.80665,
      noisy_accel)

check("too few samples is safe", fit_motion(samples[:2], 0) == (0.0, 0.0))
check("identical timestamps are safe",
      fit_motion([(1.0, (5.0, 0, 0))] * 6, 0) == (0.0, 0.0))

print()
if failures:
    print("%d FAILED" % len(failures))
    sys.exit(1)
print("all passed")
