# G-Rebels → SimHub telemetry

Motion telemetry for [G-Rebels](https://store.steampowered.com/app/2445980/GRebels/),
so a motion platform can follow your craft.

The game has no telemetry output. Its FAQ says a UDP port for motion simulators
is *planned* for Early Access, but it isn't there yet. This reads the craft out
of the running game and streams it to SimHub — motion, attitude and speed, plus
weapons, heat, shields and boost.

G-Rebels appears in SimHub under **its own name and icon**, not disguised as a
racing game.

> **Beta.** Validated against live flight on one machine and one game build.
> Please open an issue if it misbehaves.

---

## What you need

* G-Rebels on Steam (Windows)
* SimHub, on this PC or another machine on your network
* Nothing else — the app installs the rest

## Quick start

1. Download `GRebelsTelemetry-x.y.z.msi` from
   [Releases](../../releases) and run it. It installs the app, adds the G Rebels
   definition to SimHub, and — if you tick the box — fetches the AZOM plugin for
   MOZA AB9 force feedback. See [Installer](#installer) for what each step does.
2. Launch **G-Rebels Telemetry** and press **Install / repair**. That fetches
   UE4SS, applies the UE 5.8 compatibility files, and drops in the telemetry mod.
3. **Restart SimHub**, then pick **G Rebels** in its games list. If SimHub is on
   this same PC the definition was installed for you in step 2; for a separate
   rig see [below](#if-simhub-is-on-a-different-machine).
4. Start G-Rebels and load into a flight.
5. Back in the app, enter your SimHub machine's IP (or leave `127.0.0.1` if it's
   the same PC) and press **Start streaming**.

The window shows your craft, speed, altitude, packet rate and how fast the game
is updating. The taskbar title tracks your speed, so you can see at a glance
that it's live while the game is fullscreen.

### Installer

The MSI asks two things: where SimHub lives, and whether you want AB9 force
feedback. Both are optional — leave them alone and you get a working telemetry
app, nothing more.

**The SimHub folder is only needed for AZOM.** The G Rebels definition installs
into your user profile regardless, so it needs no path and no elevation. The box
is pre-filled by searching for `SimHubWPF.exe` rather than a folder called
"SimHub", so an empty directory left over from an uninstall can't fool it. Don't
have SimHub? The dialog links to [its download page](https://www.simhubdash.com/download-2/?downloadnow=1).

**AZOM is off by default.** Ticking it downloads the newest *stable* release
from [AZOM's own repository](https://github.com/giantorth/AZOM/releases) and
places `MozaPlugin.dll` in SimHub. Specifics worth knowing:

* It resolves `/releases/latest`, not the top of the release list. Three of the
  four most recent entries on that repo are PR prereleases — "newest" would put
  an untested build on your rig.
* The archive is checked before anything is written into Program Files: exactly
  one DLL, no path separators in the entry name, a plausible size, and a real
  `MZ` header. A release that changes shape gets refused rather than trusted.
* **Close SimHub first.** It holds the plugin open, and the install refuses
  rather than half-replacing it.
* Your previous `MozaPlugin.dll` is kept as `MozaPlugin.dll.bak`.
* SimHub will ask you to enable the plugin the next time it starts.

AZOM is GPL-3.0 and this project is MIT, so its binary is **never bundled**. The
installer fetches it from upstream, which leaves you obtaining it from the
authors and us merely putting it in place.

### If SimHub is on a different machine

SimHub reads its game definitions from its **own** disk, so two files have to
travel. Copy both of these from this PC:

```
%LocalAppData%\SimHub\ExternalSims\Definitions\G Rebels.simdef
%LocalAppData%\SimHub\ExternalSims\Definitions\icon.png
```

into the identical folder on the rig, then restart SimHub. They must stay
together — `IconPath` inside the definition is relative. Nothing else transfers.

Or just run the MSI on the rig too and let it do the copying: the telemetry app
it also installs is harmless there, and it means the AZOM option is available on
the machine that actually has your stick plugged in.

Then allow the packets through the firewall **on the SimHub machine**:

```powershell
New-NetFirewallRule -DisplayName "G-Rebels telemetry" `
  -Direction Inbound -Protocol UDP -LocalPort 30777 -Action Allow -Profile Private
```

If Windows has your LAN marked as Public, use `-Profile Any`. A blocked port is
the most common reason for "everything looks fine but nothing moves".

Automatic game detection won't work in this setup — SimHub can't see a process
on another machine — so select G Rebels by hand. Everything else is unaffected.

---

## How it works

```
   GAME PC                                        SIMHUB / RIG
   ┌────────────────────────────────┐             ┌────────────────────┐
   │ G-Rebels                       │             │ SimHub             │
   │   └ UE4SS ─ GRTelemetry mod    │  UDP 30777  │   ├ Motion plugin  │
   │        publishes addresses     │ ──────────► │   ├ ShakeIt        │
   │        + property offsets      │   (native)  │   └ AZOM ─► AB9    │
   │                                │             │                    │
   │ GRebelsTelemetry.exe           │  UDP 20777  │  G Rebels appears  │
   │   reads memory, sends packets  │ ──────────► │  as its own game   │
   └────────────────────────────────┘  (DR2 compat)└───────────────────┘
```

The in-game mod does almost nothing: a few times a second it writes out the
*addresses* of the player pawn, its root component and the UWorld. All the fast
sampling happens outside the game via `ReadProcessMemory`, so the game pays no
per-frame cost.

**Offsets are discovered, not hardcoded.** The mod also publishes the transform
values it read through Unreal's reflection system, which gives a known-answer
key: the app scans the object for the bytes matching that value and the offset
falls out. A game patch that shifts the layout costs a re-scan of a few hundred
milliseconds, not a fresh reverse-engineering session.

**Samples are timestamped with the game's own simulation clock**, not wall
clock. This turned out to matter more than anything else. Measured against a
recorded trace, the craft advances a near-constant distance per game update
while wall-clock intervals between those updates scatter by ±40% — the
correlation between distance travelled and measured interval is 0.04, i.e.
none. Dividing good distances by bad intervals produced double-digit phantom G
in early versions.

### Two output formats

**Native (default, UDP 30777).** SimHub's External Sim integration: a `.simdef`
definition declaring exactly what the game provides, and a 211-byte binary
packet. Seven fields are SimHub *standard* fields — pitch, yaw, roll, speed and
the three body-frame accelerations — which is what makes the Motion plugin work
at all; custom fields are exposed as properties but never reach SimHub's
internal telemetry model. The rest carry weapons, heat, shields, boost and the
synthetic engine channels.

Those standard fields come with conventions that differ from Unreal's, and
every mismatch fails *silently* — the platform still moves, just wrongly:

| SimHub wants | Unreal gives | Applied |
|---|---|---|
| `PitchDegrees` positive nose-**down** | nose-up | negated |
| `SpeedKmh` in km/h | m/s | ×3.6 |
| `Local*Ms2` in m/s² | derived in g | ×9.80665 |
| heave **excluding** gravity | — | none needed |

Heave needs no correction because velocity is differentiated kinematically, so
there is no accelerometer +g reaction term to remove.

**Compatibility (UDP 20777).** Codemasters `extradata=3` — 264 bytes, 66 floats
— carrying position, velocity and a full orientation basis. Useful for motion
software that isn't SimHub, and as a fallback. Set `output_mode` to `dr2`,
`simdef` or `both` in settings. It's the approach
[SpaceMonkey](https://github.com/PHARTGAMES/SpaceMonkey) established for
bolting unsupported games onto motion software.

### Beyond motion

The mod resolves gameplay properties **by name** through Unreal's reflection
system and publishes their offsets, so a game patch that moves them costs a
re-scan rather than silently reading garbage. The bridge reads the whole ~5 KB
span in a single `ReadProcessMemory`, which also guarantees the values share one
instant instead of smearing across the sampling window.

That gives shields, health, boost, missiles, landing gear — and firing.

**Shots come from the alternating-barrel flag, not an ammo count.** The primary
gun turns out to be heat-limited: `PrimaryFireMagazineStatus` reads 0 throughout
flight while the guns are plainly firing, and what the pilot experiences as
"magazine emptied" is the overheat cutout. `ShootLeft` flips once per shot as
the barrels alternate, which is an exact per-shot event — and it tells you
*which* barrel, so shakers can fire in stereo. Each shot also feeds a decaying
`fire_impulse` envelope, so an effect can bind strength directly instead of
doing edge detection in SimHub.

### Running UE4SS on Unreal Engine 5.8

G-Rebels is UE 5.8. UE4SS officially tops out at 5.7, and out of the box its
signature scan finds most of what it needs but fails on three AOBs — and the
scan is all-or-nothing, so it aborts. Four things make it work, all applied
automatically by **Install / repair**:

1. **Current binaries** from the latest experimental build — the tagged
   releases predate 5.8 by a long way.
2. **Engine version pinned to 5.7** in `UE4SS-settings.ini`.
3. **Signature and layout overrides** for the FName constructor,
   `FUObjectHashTables::Get` and `GNatives`.
4. **A GUObjectArray override.** Without it, UE4SS locks onto an adjacent
   global that reads zero objects and hangs on *"Waiting for object
   construction"* forever, with no error message.

Full details in [docs/BUILD-NOTES.md](docs/BUILD-NOTES.md).

---

## Force feedback: MOZA AB9 via AZOM

If you have a MOZA AB9 and the [AZOM](https://github.com/giantorth/AZOM) plugin,
the stick can be driven from this telemetry — but only through car-shaped
inputs. AZOM sets the AB9's vibration period from `rpm / maxRpm` and fires its
kick on gear-*string* transitions.

G-Rebels has neither an engine nor a gearbox, so those channels are synthesised:
RPM leans mostly on airspeed with a boost contribution, so the buzz rises as you
accelerate and jumps when the booster lights. The denominator prefers the game's
own `CurrentMaxVelocity` (700 km/h), range-checked because it arrives in Unreal
centimetres and is occasionally zero.

Gear is pinned to neutral by default. AZOM kicks the stick on **every** gear
change, so a synthetic gear derived from speed would thump you each time you
accelerated through a band. Set `synth_gear` to `true` if that appeals.

Set `synth_engine` to `false` to send nothing at all on those channels.

---

## Known limitations

**G-forces are derived, not read.** `GetVelocity()` is computed rather than
stored — a 16 KB scan of both the pawn and its root component, in float and
double layouts, found nothing — so acceleration is fitted from position alone.
Differentiating twice squares the jitter, and roughly 10% of frames still reach
the 6 g clamp during fast flight. The clamp count is shown in the window rather
than hidden.

The default 0.30 s fit window recovers the true mean to within 3% of a
long-window reference; shortening it makes the platform buzz, lengthening it
adds cue latency. Tune `fit_window_s` in
`%APPDATA%\GRebelsTelemetry\settings.json`, or turn **Send G-forces** off
entirely — position and orientation are unaffected, so tilt cueing still works.

**The sim clock does not tick on every frame.** Around 22% of position updates
arrive with the clock unchanged. Two positions sharing one timestamp is a
division by zero in disguise; before this was handled it produced spikes to
1661 m/s² — 169 g — and pinned every acceleration axis at the clamp. The
sampler now keeps one sample per tick, which is also what the fit always
assumed it had.

**Offsets are per-build.** They're re-derived on each connect, but a large
enough engine change could defeat the scan. It falls back to known offsets and
says so in the window.

**Windows only.** It reads another process's memory through the Win32 API.

---

## Building from source

```powershell
git clone https://github.com/jkliman/grebels-simhub-telemetry
cd grebels-simhub-telemetry
powershell -ExecutionPolicy Bypass -File tools\build_msi.ps1
```

Produces `dist\GRebelsTelemetry-x.y.z.msi`. The WiX 3.14 toolset is fetched on
first run — it's a plain zip needing no .NET SDK, unlike WiX 4+ which is a
dotnet tool. `tools\build_exe.ps1` builds just the app if you don't want an
installer. To run without building anything:

```powershell
python run_app.py
```

Needs Python 3.10+ (tkinter ships with the standard Windows installer). No
third-party runtime dependencies.

---

## Is this allowed?

It reads memory from a single-player game you own, and never writes to it — the
process handle is opened read-only. It does not modify game files; UE4SS and the
mod are added alongside them and **Remove** takes them back out. There's no
anti-cheat in G-Rebels.

That said, the *right* long-term answer is native telemetry from the developers,
who have already said they plan it. [docs/DEV-REQUEST.md](docs/DEV-REQUEST.md)
is a concrete spec worth sending them — if it ships, this project becomes
unnecessary, which would be a good outcome.

---

## Credits

* [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) — the scripting system that
  makes any of this possible. Downloaded at install time, not redistributed.
* The UE4SS maintainers and the contributors to issue
  [#1379](https://github.com/UE4SS-RE/RE-UE4SS/issues/1379), whose UE 5.8
  signature and layout files are vendored in `ue4ss-5.8/` and who documented
  the GUObjectArray failure mode.
* [SpaceMonkey](https://github.com/PHARTGAMES/SpaceMonkey) — for establishing
  the Codemasters-UDP-as-universal-transport approach.
* [SimHub](https://www.simhubdash.com/).

Not affiliated with Reakktor Studios, Senatis GmbH, or any of the above.

## License

MIT — see [LICENSE](LICENSE).
