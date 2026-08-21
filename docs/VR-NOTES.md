# Telemetry and VR

Short version: UE4SS cannot be loaded while a VR runtime is, so the telemetry
stopped depending on UE4SS.

## The crash

With a headset attached, G-Rebels starts in VR and loads the Meta OpenXR
runtime into the process. UE4SS and that runtime cannot coexist — the game dies
during startup:

```
EXCEPTION  0xC0000005 ACCESS_VIOLATION
fault      reading 0x0000000000000008   (null-pointer dereference)
module     UE4SS.dll +0x3CE081
```

Nineteen Oculus DLLs were loaded at the moment of the crash: hand tracking,
body tracking, passthrough, the full OpenXR plugin set.

Both UE4SS and the OpenXR runtime detour the engine's rendering and map-loading
paths. When two detour libraries patch the same functions, one ends up
following a pointer the other has invalidated. UE4SS's own log shows its
`LoadMap` detour failing to install even on a machine where the game runs fine
— harmless there because nothing competes, fatal once the VR runtime does.

## Why it was slow to spot

The game machine and the sim rig were byte-identical everywhere it was easy to
look, so each difference got ruled out in turn:

| | flat-screen PC (works) | rig (crashes) |
|---|---|---|
| Game build | SizeOfImage `0xA7AA000` | identical |
| `UE4SS.dll` | 16,475,648 bytes, SHA `d7e7826d` | identical |
| Override files | 4 signatures + 2 layout inis | identical |
| Engine pin | Major 5 / Minor 7 | identical |
| Windows | 10.0 build 26200 | identical |
| **OpenXR runtime** | **none installed, no headset** | **Meta/Oculus, headset attached** |

Every successful run was flat-screen. The one variable never tested was the one
that mattered. Two isolating tests then settled it: disabling only the Lua mod
still crashed, so the mod was not at fault; renaming `dwmapi.dll`, which is how
UE4SS loads itself, stopped the crash entirely.

## The fix

UE4SS never read any telemetry. Its only job was to publish the player pawn's
address, its root component and the UWorld once per session, and to resolve
gameplay property offsets by name. All the actual sampling was already external
`ReadProcessMemory`.

So the pawn is now found from outside instead: the global holding the current
UWorld sits at a fixed offset in the executable, and the craft is two pointer
hops from there. Nothing is injected, so there is nothing to collide with the
headset runtime.

## Deriving the route again

If a game update moves things, the route can be re-derived on a **flat-screen**
machine with UE4SS installed, which is safe there:

```powershell
py -3 tools\ptrscan.py --target "<game>\G_Rebels\Binaries\Win64\gr_target.txt"
py -3 tools\ptrscan.py --target ... --anatomy
py -3 tools\compare_resolve.py --seconds 600
```

`ptrscan` scans the executable for whatever holds the world address UE4SS
reports, then searches for pointer routes from the world to the pawn.
`--anatomy` locates the root component, the sim clock and the transform.
`compare_resolve` then runs both sources side by side for as long as you fly and
reports any disagreement — fly, die, respawn and change level, because those
transitions are where a lazy route goes stale without saying so.

Put the results in `src/grebels_telemetry/resolve.py`. Keep at least two routes
whose **first hops differ**: routes sharing a first hop pass through the same
object and are one witness, not two.
