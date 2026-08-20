"""Discover the memory offsets we need, instead of hardcoding them.

The UE4SS mod publishes both the ADDRESSES of the objects we care about and the
VALUES it read out of them through Unreal's reflection system. That gives us a
known-answer key: scan the object for the bytes matching the value the mod
reported, and the offset falls out.

This matters because a game patch, or a different build of Unreal, moves these
offsets. Re-running a scan that takes a few hundred milliseconds beats asking
every user to wait for someone to reverse-engineer new constants.

Offsets observed on G-Rebels build 24536137 (UE 5.8) are used as fallbacks and
as a first guess to verify, which makes the common case nearly free.
"""

import struct
import time

# Known-good values for the build this was developed against.
FALLBACK_LOCATION = 0x148     # USceneComponent::RelativeLocation, 3 x f64, cm
FALLBACK_ROTATION = 0x160     # USceneComponent::RelativeRotation, 3 x f64, deg
FALLBACK_WORLD_TIME = 0x180   # UWorld::TimeSeconds, f64

COMPONENT_SCAN_BYTES = 0x600
WORLD_SCAN_BYTES = 0x2000

# An FRotator follows the FVector immediately in USceneComponent.
ROTATION_GAP = 0x18


class CalibrationError(RuntimeError):
    pass


def _triple_matches(raw, offset, want, tolerance):
    try:
        a, b, c = struct.unpack_from("<3d", raw, offset)
    except struct.error:
        return False
    return (abs(a - want[0]) <= tolerance
            and abs(b - want[1]) <= tolerance
            and abs(c - want[2]) <= tolerance)


def find_vector_offset(process, base, want, tolerance, scan_bytes=COMPONENT_SCAN_BYTES,
                       first_guess=None):
    """Offset of a 3 x float64 vector inside the object at `base`."""
    raw = process.read(base, scan_bytes)
    if raw is None:
        raise CalibrationError("could not read component memory")

    if first_guess is not None and _triple_matches(raw, first_guess, want, tolerance):
        return first_guess, True

    hits = [off for off in range(0, scan_bytes - 24, 8)
            if _triple_matches(raw, off, want, tolerance)]
    if not hits:
        raise CalibrationError("no vector in the object matched the published value")
    # Prefer the known layout when several candidates match; the component's
    # world transform also contains the translation and can alias.
    if first_guess in hits:
        return first_guess, True
    return hits[0], False


def find_world_time_offset(process, world_base, want, tolerance=0.5,
                           settle=0.35, first_guess=FALLBACK_WORLD_TIME):
    """Offset of UWorld::TimeSeconds.

    Two passes: candidates near the published value, then keep only those that
    advanced by roughly the elapsed wall time. Several doubles in UWorld sit
    near the clock (real time, audio time); requiring the right RATE, not just
    the right value, is what separates them.
    """
    raw = process.read(world_base, WORLD_SCAN_BYTES)
    if raw is None:
        raise CalibrationError("could not read UWorld memory")

    candidates = []
    for off in range(0, WORLD_SCAN_BYTES - 8, 8):
        value = struct.unpack_from("<d", raw, off)[0]
        if abs(value - want) <= tolerance:
            candidates.append((off, value))
    if not candidates:
        raise CalibrationError("no clock-like double found in UWorld")

    started = time.perf_counter()
    time.sleep(settle)
    elapsed = time.perf_counter() - started
    raw2 = process.read(world_base, WORLD_SCAN_BYTES)
    if raw2 is None:
        raise CalibrationError("could not re-read UWorld memory")

    advancing = []
    for off, before in candidates:
        after = struct.unpack_from("<d", raw2, off)[0]
        delta = after - before
        # allow for a paused game (delta 0 fails) and for frame-time wobble
        if abs(delta - elapsed) <= max(0.05, elapsed * 0.35):
            advancing.append((off, abs(delta - elapsed)))
    if not advancing:
        raise CalibrationError(
            "found clock-like values but none advanced in real time "
            "(is the game paused or in a menu?)")

    advancing.sort(key=lambda item: (item[0] != first_guess, item[1]))
    return advancing[0][0]


def calibrate(process, target, speed_hint=150.0, staleness=0.25):
    """Work out every offset we need from one published target snapshot.

    `speed_hint` (m/s) sizes the position tolerance: the mod publishes at a few
    hertz, so a fast-moving craft has already travelled some distance by the
    time we scan. Being generous is safe -- a false positive would have to be
    three consecutive doubles all landing near a very specific point in space.
    """
    result = {}

    location = target.get("loc")
    if not location:
        raise CalibrationError("the mod did not publish a location to match against")

    tolerance_cm = max(500.0, speed_hint * 100.0 * staleness * 2.0)
    loc_offset, exact = find_vector_offset(
        process, target["root_addr"], location, tolerance_cm,
        first_guess=FALLBACK_LOCATION)
    result["location"] = loc_offset
    result["location_was_expected"] = exact

    rotation = target.get("rot")
    rot_offset = None
    if rotation:
        try:
            rot_offset, _ = find_vector_offset(
                process, target["root_addr"], rotation, 5.0,
                first_guess=loc_offset + ROTATION_GAP)
        except CalibrationError:
            rot_offset = None
    result["rotation"] = rot_offset if rot_offset is not None else loc_offset + ROTATION_GAP
    result["rotation_was_matched"] = rot_offset is not None

    world_base = target.get("world_addr")
    published_time = target.get("time_seconds")
    if world_base and published_time is not None and published_time >= 0:
        result["world_time"] = find_world_time_offset(
            process, world_base, published_time)
    else:
        result["world_time"] = FALLBACK_WORLD_TIME
    return result
