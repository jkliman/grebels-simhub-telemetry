# Running this on your sim rig

Short answer: **you don't install anything on the rig.** The rig only needs
SimHub listening on a UDP port. Everything else — UE4SS, the mod, Python, the
bridge — lives on the game PC, because reading the craft's transform out of the
game's memory has to happen where the game is running.

```
   GAME PC (omega, 10.0.0.105)                    SIM RIG
   ┌──────────────────────────────┐               ┌──────────────────────┐
   │ G-Rebels + UE4SS + GRTelemetry│              │ SimHub               │
   │              │                │   UDP 20777  │   └ Motion plugin    │
   │              ▼                │ ───────────► │        └ platform    │
   │ gr_bridge.py --host <RIG_IP>  │   your LAN   │                      │
   └──────────────────────────────┘               └──────────────────────┘
```

## On the rig — two things

**1. Point SimHub at DiRT Rally 2.0.**
Games → DiRT Rally 2.0, UDP port `20777`. SimHub just listens on that port; the
real DiRT Rally 2.0 does not need to be installed and its process does not need
to exist. This is the same trick SpaceMonkey uses for unsupported games.

**2. Let the packets through the firewall.**
In an admin PowerShell on the rig:

```powershell
New-NetFirewallRule -DisplayName "SimHub telemetry UDP 20777" `
  -Direction Inbound -Protocol UDP -LocalPort 20777 -Action Allow -Profile Private
```

If your LAN is marked Public in Windows, change `-Profile Private` to `Any`.
This is the single most likely thing to silently eat your telemetry.

That's the whole rig side. No Python, no UE4SS, no mod.

## On the game PC — already done

Installed and working:

* `E:\SteamLibrary\...\G_Rebels\Binaries\Win64\` — UE4SS (`dwmapi.dll`,
  `UE4SS.dll`, `UE4SS-settings.ini`, signature and layout files) plus
  `Mods\GRTelemetry\`
* `Downloads\GRTelemetry\gr_bridge.py` — the bridge
* Python 3.14.6

**Admin is not required** — the bridge reads the game's memory as your normal
user account. Verified.

## Running it

1. Start G-Rebels and load into a flight.
2. On the game PC, double-click `run_bridge.bat` (edit the IP inside it once),
   or from a terminal:

   ```
   python gr_bridge.py --source ue4ss --host 192.168.x.x
   ```

3. You should see it print `tracking BP_PAWN_GR_PLAYER_C` followed by a live
   speed/altitude/G readout. SimHub on the rig should light up within a second.

Order doesn't really matter — the bridge waits for the game and re-acquires the
craft automatically if you die, respawn or change level.

## Checking it before you trust the platform

Run the bridge with `--source synth` first. That generates a synthetic flight —
banked turns, climbs, rolls — with no game running at all, which separates
"telemetry isn't arriving" from "the game data is wrong":

```
python gr_bridge.py --source synth --host 192.168.x.x
```

If the rig moves on synth but not on `ue4ss`, the problem is the game side. If
it doesn't move on synth either, it's SimHub config or the firewall.

## Useful flags

| Flag | Default | What it's for |
|---|---|---|
| `--host` | 127.0.0.1 | rig IP |
| `--port` | 20777 | must match SimHub |
| `--rate` | 100 | send rate, Hz. SimHub wants ≥60 |
| `--gclamp` | 6.0 | ceiling on reported G — lower it if the platform slams |
| `--vel-window` / `--acc-window` | 0.05 / 0.10 | smoothing vs latency on the derived G |
| `--record fly.csv` | — | log a session |
| `--source replay --replay-file fly.csv` | — | replay it, for repeatable motion tuning |

`--record` then `--source replay` is the honest way to tune a motion profile:
same flight every time, so you're comparing profile changes rather than
comparing two different flights.

## When something breaks

**No data at all** → firewall first, then confirm SimHub is on DiRT Rally 2.0
and port 20777, then try `--source synth` to isolate which side is at fault.

**Bridge says the game isn't running** → it matches on
`G_Rebels-Win64-Shipping.exe`. If Steam launched the shim only, wait for the
real process.

**Bridge starts but never prints `tracking`** → the mod isn't publishing.
Check `Binaries\Win64\gr_target.txt` has a recent timestamp, and look for
`[GRTelemetry]` lines in `UE4SS.log`.

**After a game update** → the memory offsets will likely move. Don't
reverse-engineer them again; re-run the calibration scripts, which find them by
matching values the mod reads through reflection. Takes seconds. Details in the
build notes.

## Still outstanding

The G-force channel is derived from position and isn't trustworthy yet — it
still flips sign about 15×/sec. Position and orientation are solid, so tilt
cueing will feel right; surge/sway may buzz. If it does, raise `--acc-window`
to 0.2 and drop `--gclamp` to 3 as a stopgap.
