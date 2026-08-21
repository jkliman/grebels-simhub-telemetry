"""Find the player's craft in memory without injecting anything into the game.

UE4SS did this job by living inside the process and asking Unreal politely.
That works, but it cannot coexist with a VR runtime: the OpenXR layer and
UE4SS both try to detour the same engine functions, and the game dies on
startup. So instead of asking from the inside, we walk to the craft from the
outside.

The route starts at a fixed address in the executable's own data -- the global
that Unreal keeps the current UWorld in -- and takes two pointer hops to reach
the pawn. Three independent routes are walked and their answers compared,
because a single route that happens to pass through a cache would fail
silently; three routes disagreeing is a signal we can act on.

Nothing here writes to the game. It is the same read-only handle the sampler
already uses.
"""

import math
import struct

# Where Unreal keeps the current world, as an offset into the game executable.
# Found by scanning the module for the address UE4SS reported (tools/ptrscan.py)
# and confirmed to be the only match, unchanged across restarts.
GWORLD_RVA = 0x9C865B0

# Two hops from the world to the pawn. Each entry is a chain of offsets to
# follow; they were discovered by search and kept only after surviving a
# restart. They are walked together and voted on.
ROUTES = (
    (0x1A8, 0x3E8),
    (0xC20, 0x320),
    (0xC20, 0xB20),
)

ROOT_COMPONENT_OFFSET = 0x1B8    # AActor::RootComponent
WORLD_TIME_OFFSET = 0x180        # UWorld::TimeSeconds, f64
LOCATION_OFFSET = 0x148          # USceneComponent::RelativeLocation, 3 x f64, cm
ROTATION_OFFSET = 0x160          # USceneComponent::RelativeRotation, 3 x f64, deg

# The craft's Blueprint properties, at the offsets UE4SS resolved by name.
# These belong to BP_PAWN_GR_PLAYER_C and are as stable as the routes above:
# both are layout facts about one build of the game.
FIELDS = {
    "Health": (2432, "DoubleProperty"),
    "PrimaryFireMagazineSize": (2476, "IntProperty"),
    "PrimaryFireMagazineStatus": (2488, "IntProperty"),
    "ShieldHealthMax": (2520, "DoubleProperty"),
    "ShieldHealthCurrent": (2704, "DoubleProperty"),
    "LandingGearActive": (3120, "BoolProperty"),
    "CurrentMaxVelocity": (3224, "DoubleProperty"),
    "PrimaryFire_pressed": (3441, "BoolProperty"),
    "BoostAxis": (3504, "DoubleProperty"),
    "PrimaryFire_Success": (3548, "BoolProperty"),
    "AvailableMissiles": (4548, "IntProperty"),
    "EngineBoostTimePercentage": (4640, "DoubleProperty"),
    "MaxAvailableMissiles": (4960, "IntProperty"),
    "isLanding": (5632, "BoolProperty"),
    "TotalHeatPrimary": (5928, "DoubleProperty"),
    "MaxHeatPrimary": (5964, "FloatProperty"),
    "isOverheatedPrimary": (5968, "BoolProperty"),
    "EngineBoosterIsActive": (6016, "BoolProperty"),
    "Force F Primary Fire": (6208, "DoubleProperty"),
    "Force F Secondary Fire": (6216, "DoubleProperty"),
    "MissileNotificationActive": (6812, "BoolProperty"),
    "CurrentVelocity": (6976, "DoubleProperty"),
    "ShootLeft": (7060, "BoolProperty"),
}

CRAFT_NAME = "BP_PAWN_GR_PLAYER_C"
WORLD_LIMIT_CM = 1e9             # anything further out is not a position


class ResolveError(RuntimeError):
    pass


def _qword(process, address):
    raw = process.read(address, 8)
    if raw is None:
        return None
    value = struct.unpack("<Q", raw)[0]
    return value or None


class Resolver:
    """Walks the pointer routes and vouches for what it finds."""

    def __init__(self, process, gworld_rva=GWORLD_RVA, routes=ROUTES):
        self.process = process
        self.gworld_rva = gworld_rva
        self.routes = tuple(routes)
        found = process.module_range()
        if not found:
            raise ResolveError("could not locate the game module")
        self.module_base, self.module_size = found
        self.module_end = self.module_base + self.module_size
        self.last_votes = ()

    # -- checks -------------------------------------------------------------
    def _is_object(self, address):
        """True if this looks like a live Unreal object: a vtable in the exe."""
        if not address or address % 8:
            return False
        head = _qword(self.process, address)
        return bool(head and self.module_base <= head < self.module_end)

    def _plausible_position(self, root):
        raw = self.process.read(root + LOCATION_OFFSET, 24)
        if raw is None:
            return False
        for value in struct.unpack("<3d", raw):
            if not math.isfinite(value) or abs(value) > WORLD_LIMIT_CM:
                return False
        return True

    def vouch(self, pawn):
        """Is this address really a flyable craft, and where is its transform?

        Three cheap tests, in the order that rejects fastest: the object has an
        Unreal vtable, it owns a root component that also does, and that
        component holds a position a craft could actually be at. A stale
        pointer left over from a previous flight fails the first or second; a
        pointer into unrelated memory almost always fails all three.
        """
        if not self._is_object(pawn):
            return None
        root = _qword(self.process, pawn + ROOT_COMPONENT_OFFSET)
        if not self._is_object(root):
            return None
        if not self._plausible_position(root):
            return None
        return root

    # -- the walk -----------------------------------------------------------
    def world(self):
        return _qword(self.process, self.module_base + self.gworld_rva)

    def _follow(self, start, offsets):
        address = start
        for offset in offsets:
            address = _qword(self.process, address + offset)
            if not address:
                return None
        return address

    def pawn(self, world):
        """The address the most routes agree on, or None.

        Ties go to whichever candidate passes vouch(); if several do, the one
        with the most votes wins. Requiring agreement is what stops a single
        stale route from quietly steering the whole bridge at a dead craft.
        """
        votes = {}
        for offsets in self.routes:
            candidate = self._follow(world, offsets)
            if candidate:
                votes[candidate] = votes.get(candidate, 0) + 1
        self.last_votes = tuple(sorted(votes.items(), key=lambda kv: -kv[1]))
        for candidate, _count in self.last_votes:
            if self.vouch(candidate):
                return candidate
        return None

    def snapshot(self):
        """The same shape the UE4SS mod publishes, read straight from memory."""
        world = self.world()
        if not world:
            raise ResolveError("the world pointer is empty - is a flight loaded?")
        pawn = self.pawn(world)
        if not pawn:
            raise ResolveError("no craft found - load into a flight")
        root = self.vouch(pawn)

        raw = self.process.read(root + LOCATION_OFFSET, 24)
        loc = struct.unpack("<3d", raw) if raw else None
        raw = self.process.read(root + ROTATION_OFFSET, 24)
        rot = struct.unpack("<3d", raw) if raw else None
        clock = self.process.read_double(world + WORLD_TIME_OFFSET)

        return {
            "pawn": CRAFT_NAME,
            "pawn_addr": pawn,
            "root_addr": root,
            "world_addr": world,
            "time_seconds": clock if clock is not None else -1.0,
            "loc": loc,
            "rot": rot,
            "vel": None,
            "fields": dict(FIELDS),
            "offsets": {
                "location": LOCATION_OFFSET,
                "rotation": ROTATION_OFFSET,
                "world_time": WORLD_TIME_OFFSET,
            },
        }
