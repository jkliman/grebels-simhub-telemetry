# Telemetry output request — G-Rebels

*Draft message for the Steam forum / Reakktor + Senatis. Adjust the tone to taste.*

---

Hi — the FAQ says a telemetry/UDP data port for motion simulators is planned for
Early Access. That's great news, and I'd like to offer a concrete spec so it
lands in a form the existing motion-sim ecosystem can consume on day one. There
is a real audience for this: G-Rebels in VR on a motion platform is a killer
demo, and right now nobody can do it.

**The short version:** one UDP struct, sent at 100 Hz from the craft's tick,
containing seven things. Nothing else is needed.

| Field | Notes |
|---|---|
| Timestamp | seconds, float64 or float32, monotonic |
| World position | X, Y, Z — Unreal cm is fine, just document it |
| Orientation | either a quaternion, or the craft's forward + right unit vectors (preferred — no ambiguity about euler order) |
| Linear velocity | world-space, cm/s or m/s |
| Angular velocity | body-frame roll/pitch/yaw rates, deg/s |
| Linear acceleration | body-frame surge/sway/heave — you already have this in the physics solver, and it is far more accurate than anything we can differentiate externally |
| Flags | on-ground / in-menu / paused / craft-active, so the rig can settle when I'm not flying |

Optional extras that dashboards would use: airspeed, altitude, throttle, boost,
damage/hull, weapon fire events, collision impulse.

**Implementation notes**

* A plain `sendto()` on a `FSocket` from the pawn tick is enough — no plugin
  needed. Destination IP + port + enable flag in `GameUserSettings.ini` covers
  the common case where SimHub runs on a second machine.
* Please make the IP configurable, not localhost-only. Most motion rigs run
  their control software on a separate PC.
* SimHub has an official integration contract for exactly this case —
  <https://manual.simhubdash.com/external-sim-integration> — including a
  `.simdef` definition file and a C++ struct generator. If you ship a `.simdef`
  with the game, SimHub picks it up automatically and every SimHub user gets
  motion, dash and haptics for free. That is probably the single highest-value
  hour of work available here.
* Keep the struct append-only and version-stamped so tools don't break on patches.

Happy to test any build and report back — I have a motion platform and SimHub
set up and can validate the feed the day it exists.
