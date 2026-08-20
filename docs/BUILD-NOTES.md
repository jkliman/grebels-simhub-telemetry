# G-Rebels telemetry — working build notes

*Live as of 20 Aug 2026, game build 24536137. Everything below is verified on
this machine, not theoretical.*

## Status

| Piece | State |
|---|---|
| UE4SS on UE 5.8 | **working** |
| Player craft discovery | **working** — `BP_PAWN_GR_PLAYER_C` |
| Position + orientation at 100 Hz | **working** |
| Sim-clock timestamps | **working** |
| UDP → SimHub (DiRT Rally 2.0 format) | **working**, 264-byte packets verified |
| G-force channel | **working** — quadratic fit, validated against a recorded flight |

## Getting UE4SS onto UE 5.8

UE4SS does not officially support 5.8 (5.7 is the ceiling). It works anyway with
three pieces:

1. **Binaries** — the nightly CMake artifact from the `Make Experimental Release`
   workflow on `UE4SS-RE/RE-UE4SS` main. The tagged releases are from Dec 2024
   and are too old. `dwmapi.dll` + `UE4SS.dll` go directly in
   `G_Rebels\Binaries\Win64\` (flat layout, no `ue4ss/` subfolder), alongside
   `UE4SS-settings.ini` and `Mods\` from the repo's `assets\` folder.

2. **Engine version override** — in `UE4SS-settings.ini`:
   ```ini
   [EngineVersionOverride]
   MajorVersion = 5
   MinorVersion = 7
   ```

3. **Signatures** — out of the box, PatternSleuth finds EngineVersion,
   GUObjectArray, GMalloc, FName::ToString, StaticConstructObject_Internal,
   ConsoleManagerSingleton and GameEngineTick, but fails on three, and the scan
   is all-or-nothing so it aborts. The `MemberVariableLayout.ini`,
   `VTableLayout.ini` and `UE4SS_Signatures\*.lua` from the maintainer's
   Sinking City 2 config (issue #1379) cover them. All three of those byte
   patterns match the G-Rebels binary **uniquely** — the two 5.8 builds share
   codegen for these engine functions.

4. **GUObjectArray override** — the built-in scan locks onto an adjacent global
   that reads `NumElements = 0`, which makes UE4SS hang forever on
   "Waiting for object construction". Confirmed here: the scan's address was
   0x68 above the real array. Fix is `UE4SS_Signatures\GUObjectArray.lua`:
   ```lua
   function Register()
       return "48 8B 05 ?? ?? ?? ?? 48 8B 14 D0 4A 8D 3C C2"
   end
   function OnMatchFound(MatchAddress)
       return MatchAddress + 0x7 + DerefToInt32(MatchAddress + 0x3)
   end
   ```
   The anchor is the GUObjectArray reference inside
   `FUObjectArray::AllocateUObjectIndex`; the chunk-index idiom after the load
   is what makes it unique.

## Memory layout (this build)

Note that UE 5.8's `FUObjectArray` starts with the chunked array — the classic
"ints first, `ObjObjects` at +0x10" layout does **not** hold. Verified live:
`Objects` ptr at +0x00, `NumElements` +0x08, `MaxElements` +0x0C,
`NumChunks` +0x10, `MaxChunks` +0x14.

| What | Address | Type |
|---|---|---|
| `USceneComponent::RelativeLocation` | `root + 0x148` | 3 × f64, Unreal cm |
| `USceneComponent::RelativeRotation` | `root + 0x160` | 3 × f64, degrees (P,Y,R) |
| `UWorld::TimeSeconds` | `world + 0x180` | f64, sim clock |
| `AActor::RootComponent` | `pawn + 0x1B8` | TObjectPtr |

`RelativeLocation`/`RelativeRotation` were found by auto-calibration: the Lua
mod publishes the values it reads through reflection, and the bridge scans the
component's memory for the matching double triple. That spacing (0x148 → 0x160
= exactly 0x18) is textbook `FVector` → `FRotator` and confirms the hit.

`UWorld::TimeSeconds` was found the same way — scan `UWorld` for a double within
0.5 of the reflected value, then keep only candidates that advance 1.0 per
second. Two survived (`+0x180` and `+0x858`); `+0x180` is the one in use.

**Do not hardcode these across game updates.** Re-run the calibration; it takes
seconds and needs no reverse engineering.

## The mod

`Mods\GRTelemetry\Scripts\main.lua` + an empty `enabled.txt`. It runs at 5 Hz
and does nothing but *publish addresses* to `gr_target.txt` — pawn, root
component, UWorld, plus the reflected transform values used for calibration.
All the fast sampling happens outside the game, via `ReadProcessMemory`, so the
mod adds no per-frame cost.

Two UE 5.8 gotchas found the hard way: `K2_GetActorRotation()` returns a table
whose keys enumerate as `Roll, Yaw, pitch` (lowercase p), and the vector
accessors return `nil` often enough that every read needs a `pcall` guard and a
fallback. This is why the mod publishes addresses rather than values.

## The G channel: what was wrong and what fixed it

Position and orientation are exact. Acceleration has to be derived, because the
game has no velocity field we can read -- `GetVelocity()` works through
reflection but the value is not stored anywhere findable in the pawn (13 memory
candidates were checked against `d(pos)/dt`; all were false positives).

Two separate problems, found in order:

**1. Wall-clock timestamps.** In the first recorded trace the craft advanced a
near-constant distance per game update while the intervals I measured between
those updates scattered by +-40%. Correlation between distance travelled and
measured interval: **0.04**. The timestamps were noise. Fixed by reading
`UWorld::TimeSeconds` and stamping every sample with the game's own clock.

**2. Differencing twice.** This was the bigger one, and the sim clock alone did
not fix it. Chained differences amplify whatever jitter survives. Measured on a
25-second recorded flight against a long-window reference:

| Estimator | Mean reported | True mean | Sign flips/s |
|---|---|---|---|
| chained differences, 0.10 s | 10.89 G | 2.08 G | 32.8 |
| chained differences, 0.20 s | 2.42 G | 2.08 G | 6.9 |
| quadratic fit, 0.20 s | 2.41 G | 2.08 G | 8.3 |
| **quadratic fit, 0.30 s** | **2.14 G** | 2.08 G | **2.7** |
| quadratic fit, 0.40 s | 2.10 G | 2.08 G | 1.1 |

At a 0.1 s window the reported signal was **five parts noise to one part
physics**. Fitting a single least-squares quadratic through position over 0.30 s
and reading both derivatives off the polynomial recovers the true mean to within
3% and drops the flip rate by an order of magnitude. Velocity improves too:
step-to-step speed change falls from 3.75 m/s to 0.61 m/s with no change in mean.

The fit is evaluated at the newest sample rather than the window centre, which
keeps cue latency low. A centred fit would be quieter but would lag the platform
by half a window.

One thing still unexplained: even with the game's own clock, distance per update
does not correlate with elapsed sim time per update (r = -0.10 over the fast
stretch of the trace). Whatever writes `RelativeLocation` is not advancing it in
proportion to frame time -- physics substepping and render-thread interpolation
are both plausible. It no longer matters in practice, because the quadratic fit
smooths across it, but it is the loose thread if someone wants to do better.

## Files

On the game PC, `Downloads\GRTelemetry\`:

* `gr_bridge.py` — the bridge. `--source ue4ss|synth|replay`
* `record_raw3.py` — armed capture, triggers above ~50 m/s
* `calibtime.py`, `calib.py` — offset auto-calibration

Two hard-won operational notes: never `Stop-Process -Name python` on this
machine — the MCP bridge itself runs as `python.exe` and killing it severs the
connection. And launch long-running helpers with `-WindowStyle Hidden`, not
`-NoNewWindow`, or they die with the shell session that spawned them.
