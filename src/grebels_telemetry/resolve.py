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

# Routes from the world down to the pawn, best first.
#
# The first one is the meaningful one: two hops reach the APlayerController --
# confirmed by checking the address against the controller UE4SS reports -- and
# Controller::Pawn is the same field the UE4SS mod was reading all along. The
# second starts from a different slot in the world and arrives independently,
# so it can corroborate the first without sharing its failure modes.
#
# Earlier drafts also used a route through UWorld+0xC20. It agreed for two
# whole sessions and then, in the third, resolved to a run of ASCII spaces.
# That slot is not an object pointer; it just happened to hold one twice. Any
# route kept here has survived a restart, a death and a fresh flight.
CONTROLLER_ROUTE = (0x58, 0x958)     # -> APlayerController
ROUTES = (
    ("controller.Pawn", (0x58, 0x958, 0x2E8)),
    ("world.player", (0x1A8, 0x3E8)),
    ("controller.AcknowledgedPawn", (0x58, 0x958, 0x350)),
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

    def __init__(self, process, gworld_rva=GWORLD_RVA, routes=ROUTES,
                 min_witnesses=2):
        self.process = process
        self.gworld_rva = gworld_rva
        self.routes = tuple(routes)
        self.min_witnesses = min_witnesses
        found = process.module_range()
        if not found:
            raise ResolveError("could not locate the game module")
        self.module_base, self.module_size = found
        self.module_end = self.module_base + self.module_size
        self.last_votes = ()
        self._accepted = None
        self._pending = None
        self._pending_count = 0

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
        """The address two independent witnesses agree on, or None.

        Routes that share their first hop pass through the same object, so
        they are one witness, not two -- counting them separately would let a
        single stale object outvote the truth. Witnesses are therefore counted
        by distinct starting offsets.

        This matters at respawn. Watching a death live, one witness kept
        pointing at the craft that had just been destroyed while the other
        briefly returned uninitialised memory. Either one alone would have been
        believed. Requiring both to agree turns that moment into an honest "I
        do not know" for about a second, which the bridge can ride out, instead
        of a second and a half of telemetry from a corpse.
        """
        votes = {}
        primary = None
        for index, (_name, offsets) in enumerate(self.routes):
            candidate = self._follow(world, offsets)
            if not candidate:
                continue
            if index == 0:
                primary = candidate
            # Routes sharing a first hop pass through the same object, so they
            # are one witness however many of them there are.
            votes.setdefault(candidate, set()).add(offsets[0])
        ranked = sorted(votes.items(), key=lambda kv: -len(kv[1]))
        self.last_votes = tuple((address, len(heads)) for address, heads in ranked)

        # The controller's own Pawn field is the game's answer to this
        # question, so it wins whenever it points at something real.
        if primary and self.vouch(primary):
            return self._settle(primary)

        for candidate, heads in ranked:
            if len(heads) < self.min_witnesses:
                break
            if self.vouch(candidate):
                return self._settle(candidate)
        self._pending = None
        self._pending_count = 0
        return None

    def controller(self, world):
        """The APlayerController, for diagnostics."""
        return self._follow(world, CONTROLLER_ROUTE)

    def _settle(self, candidate):
        """Hold a new craft for one extra reading before believing in it.

        The routes are read one at a time, not atomically, so a swap landing
        mid-walk can briefly produce a coherent-looking wrong answer. Seeing
        the same address twice costs a few hundred milliseconds at respawn and
        removes that whole class of glitch.
        """
        if candidate == self._accepted:
            self._pending = None
            self._pending_count = 0
            return candidate
        if candidate == self._pending:
            self._pending_count += 1
        else:
            self._pending = candidate
            self._pending_count = 1
        if self._pending_count >= 2 or self._accepted is None:
            self._accepted = candidate
            self._pending = None
            self._pending_count = 0
            return candidate
        return self._accepted if self.vouch(self._accepted) else None

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
