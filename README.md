# G-Rebels → SimHub telemetry

Motion telemetry for [G-Rebels](https://store.steampowered.com/app/2445980/GRebels/),
so a motion platform can follow your craft.

The game has no telemetry output. Its FAQ says a UDP port for motion simulators
is *planned* for Early Access, but it isn't there yet. This reads the craft's
transform out of the running game and streams it to SimHub in a format SimHub
already understands.

> **Beta.** Working and validated against recorded flight data, but tested on
> one machine and one game build so far. Please open an issue if it misbehaves.

---

## What you need

* G-Rebels on Steam (Windows)
* SimHub, on this PC or another machine on your network
* Nothing else — the app installs the rest

## Quick start

1. Download `GRebelsTelemetry.exe` from
   [Releases](../../releases) and run it. No installer, no admin rights.
2. It should find your G-Rebels folder automatically. Press **Install / repair**.
   That fetches UE4SS, applies the UE 5.8 compatibility files, and drops in the
   telemetry mod.
3. In **SimHub**, choose **DiRT Rally 2.0** and set the UDP port to **20777**.
   The real DiRT Rally 2.0 does not need to be installed — SimHub just listens.
4. Start G-Rebels and load into a flight.
5. Back in the app, enter your SimHub machine's IP (or leave `127.0.0.1` if it's
   the same PC) and press **Start streaming**.

The window shows your craft, speed, altitude, packet rate and how fast the game
is updating. The taskbar title tracks your speed, so you can see at a glance
that it's live while the game is fullscreen.

### If SimHub is on a different machine

Allow the packets through the firewall **on the SimHub machine**:

```powershell
New-NetFirewallRule -DisplayName "SimHub telemetry UDP 20777" `
  -Direction Inbound -Protocol UDP -LocalPort 20777 -Action Allow -Profile Private
```

If Windows has your LAN marked as Public, use `-Profile Any`. A blocked port is
the most common reason for "everything looks fine but nothing moves".

---

## How it works

```
   GAME PC                                        SIMHUB / RIG
   ┌────────────────────────────────┐             ┌────────────────────┐
   │ G-Rebels                       │             │ SimHub             │
   │   └ UE4SS ─ GRTelemetry mod    │  UDP 20777  │   └ Motion plugin  │
   │        (publishes addresses)   │ ──────────► │                    │
   │ GRebelsTelemetry.exe           │             │                    │
   │   reads memory, sends packets  │             │                    │
   └────────────────────────────────┘             └────────────────────┘
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

Telemetry goes out as Codemasters `extradata=3` packets — 264 bytes, 66 floats —
which carry position, velocity and a full orientation basis. That's enough for a
flight model, and it means zero configuration on the SimHub side. It's the same
approach [SpaceMonkey](https://github.com/PHARTGAMES/SpaceMonkey) uses to bolt
unsupported games onto motion software.

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

## Known limitations

**G-forces are derived, not read.** The game exposes no velocity field, so
acceleration is fitted from position. The default 0.30 s fit window recovers the
true mean to within 3% of a long-window reference; shortening it makes the
platform buzz, lengthening it adds cue latency. Tune `fit_window_s` in
`%APPDATA%\GRebelsTelemetry\settings.json` if your platform prefers otherwise,
or turn **Send G-forces** off entirely — position and orientation are
unaffected, so tilt cueing still works.

**Offsets are per-build.** They're re-derived on each connect, but a large
enough engine change could defeat the scan. It falls back to known offsets and
says so in the window.

**Windows only.** It reads another process's memory through the Win32 API.

---

## Building from source

```powershell
git clone https://github.com/jkliman/grebels-simhub-telemetry
cd grebels-simhub-telemetry
powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
```

Produces `dist\GRebelsTelemetry.exe`. To run without building:

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
