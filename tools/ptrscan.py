"""Find a static pointer chain from the game module down to the player pawn.

UE4SS tells us where the pawn lives, but UE4SS cannot run alongside a VR
runtime.  So we use UE4SS once, as an oracle, to learn a route we can walk
ourselves: a fixed offset in the executable's own data (GWorld and friends)
followed by a handful of pointer hops.  Once that route is known the bridge
can find the craft with nothing but ReadProcessMemory.

    py -3 tools/ptrscan.py --target %TEMP%\\gr_target.txt

Everything here is read-only.
"""

import argparse
import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes as W

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from grebels_telemetry import memory  # noqa: E402

EXE = "G_Rebels-Win64-Shipping.exe"

MEM_COMMIT = 0x1000
PAGE_READABLE = 0x02 | 0x04 | 0x20 | 0x40      # RO, RW, EX_R, EX_RW
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", W.DWORD),
        ("__alignment1", W.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", W.DWORD),
        ("Protect", W.DWORD),
        ("Type", W.DWORD),
        ("__alignment2", W.DWORD),
    ]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("th32ModuleID", W.DWORD), ("th32ProcessID", W.DWORD),
        ("GlblcntUsage", W.DWORD), ("ProccntUsage", W.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", W.DWORD),
        ("hModule", W.HMODULE), ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]


def module_range(pid, name):
    """(base, size) of a loaded module, or None."""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x0008 | 0x0010, pid)
    if snap == -1:
        return None
    entry = _ModuleEntry()
    entry.dwSize = ctypes.sizeof(_ModuleEntry)
    wanted = name.lower()
    try:
        if k32.Module32First(snap, ctypes.byref(entry)):
            while True:
                if entry.szModule.decode(errors="ignore").lower() == wanted:
                    return int(entry.modBaseAddr), int(entry.modBaseSize)
                if not k32.Module32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return None


def committed_regions(handle, low=0, high=0x7FFFFFFFFFFF):
    """Walk the address space and yield (base, size) of readable committed memory."""
    k32 = ctypes.windll.kernel32
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    info = _MemoryBasicInformation()
    address = low
    while address < high:
        got = k32.VirtualQueryEx(handle, ctypes.c_void_p(address),
                                 ctypes.byref(info), ctypes.sizeof(info))
        if not got:
            break
        base, size = int(info.BaseAddress), int(info.RegionSize)
        if size <= 0:
            break
        usable = (info.State == MEM_COMMIT
                  and (info.Protect & PAGE_READABLE)
                  and not (info.Protect & (PAGE_GUARD | PAGE_NOACCESS)))
        if usable:
            start = max(base, low)
            end = min(base + size, high)
            if end > start:
                yield start, end - start
        address = base + size


class Space:
    """The readable parts of the process, plus a fast 'is this a pointer?' test."""

    CHUNK = 4 * 1024 * 1024

    def __init__(self, process):
        self.process = process
        self.regions = list(committed_regions(process.handle))
        self.starts = [r[0] for r in self.regions]
        self.ends = [r[0] + r[1] for r in self.regions]
        self.total = sum(r[1] for r in self.regions)

    def contains(self, address):
        if address < self.starts[0] or address >= self.ends[-1]:
            return False
        lo, hi = 0, len(self.starts) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if address < self.starts[mid]:
                hi = mid - 1
            elif address >= self.ends[mid]:
                lo = mid + 1
            else:
                return True
        return False

    def blocks(self, regions=None):
        """Yield (address, raw bytes) over the given regions, in readable chunks."""
        for base, size in (regions if regions is not None else self.regions):
            offset = 0
            while offset < size:
                take = min(self.CHUNK, size - offset)
                raw = self.process.read(base + offset, take)
                if raw is None:
                    # A page inside the region went away; fall back to page reads.
                    for page in range(0, take, 0x1000):
                        chunk = self.process.read(base + offset + page, 0x1000)
                        if chunk is not None:
                            yield base + offset + page, chunk
                else:
                    yield base + offset, raw
                offset += take

    def find_value(self, value, regions=None, limit=None):
        """Every 8-aligned address whose qword equals `value`."""
        needle = struct.pack("<Q", value)
        hits = []
        for address, raw in self.blocks(regions):
            start = 0
            while True:
                found = raw.find(needle, start)
                if found < 0:
                    break
                if (address + found) % 8 == 0:
                    hits.append(address + found)
                    if limit and len(hits) >= limit:
                        return hits
                start = found + 1
        return hits


def read_target_file(path):
    values = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def address_of(values, key):
    raw = values.get(key)
    if not raw:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


class Walker:
    """Breadth-first search for a route of pointer hops between two addresses.

    The search only steps through pointers that look like they lead somewhere
    structured: an object whose first qword is a vtable in the executable, or a
    small heap block holding such objects (a TArray's data).  That filter is
    what keeps the branching factor at tens rather than hundreds.
    """

    OBJECT_WINDOW = 0x1200
    BLOCK_WINDOW = 0x80

    def __init__(self, process, space, module_base, module_size):
        self.process = process
        self.space = space
        self.module_lo = module_base
        self.module_hi = module_base + module_size
        self.vtable_cache = {}

    def in_module(self, address):
        return self.module_lo <= address < self.module_hi

    def first_qword(self, address):
        cached = self.vtable_cache.get(address)
        if cached is None:
            raw = self.process.read(address, 8)
            cached = struct.unpack("<Q", raw)[0] if raw else 0
            self.vtable_cache[address] = cached
        return cached

    def classify(self, address):
        """'object', 'block', or None — how (and whether) to expand this pointer."""
        if address % 8 or not self.space.contains(address):
            return None
        head = self.first_qword(address)
        if self.in_module(head):
            return "object"
        if head and head % 8 == 0 and self.space.contains(head):
            if self.in_module(self.first_qword(head)):
                return "block"
        return None

    def children(self, address, kind):
        window = self.OBJECT_WINDOW if kind == "object" else self.BLOCK_WINDOW
        raw = self.process.read(address, window)
        if raw is None:
            for smaller in (0x800, 0x400, 0x100, 0x40):
                if smaller >= window:
                    continue
                raw = self.process.read(address, smaller)
                if raw is not None:
                    window = smaller
                    break
        if raw is None:
            return []
        out = []
        for offset in range(0, window - 8 + 1, 8):
            value = struct.unpack_from("<Q", raw, offset)[0]
            if value:
                out.append((offset, value))
        return out

    def search(self, roots, goal, max_depth=5, node_budget=400000):
        """Return every route from a root to `goal`, shortest first."""
        found = []
        frontier = [(address, kind, (address,), ()) for address, kind in roots]
        seen = {address for address, _ in roots}
        visited = 0
        for depth in range(max_depth):
            nxt = []
            for address, kind, chain, offsets in frontier:
                visited += 1
                if visited > node_budget:
                    print("  [walker] node budget reached at depth %d" % depth)
                    return found
                for offset, value in self.children(address, kind):
                    if value == goal:
                        found.append(offsets + (offset,))
                        continue
                    if value in seen:
                        continue
                    child = self.classify(value)
                    if child is None:
                        continue
                    seen.add(value)
                    nxt.append((value, child, chain + (value,), offsets + (offset,)))
            if found:
                return found
            frontier = nxt
            print("  [walker] depth %d: %d nodes to expand" % (depth + 1, len(frontier)))
            if not frontier:
                break
        return found


def describe(base, rva, offsets):
    parts = ["module+0x%X" % rva]
    parts.extend("0x%X" % off for off in offsets)
    return " -> ".join(parts)


def walk(process, base, rva, offsets):
    """Follow a recorded route and return the address it lands on, or None."""
    raw = process.read(base + rva, 8)
    if raw is None:
        return None
    address = struct.unpack("<Q", raw)[0]
    for offset in offsets:
        if not address:
            return None
        raw = process.read(address + offset, 8)
        if raw is None:
            return None
        address = struct.unpack("<Q", raw)[0]
    return address or None


def walk_verbose(process, base, rva, offsets):
    """Like walk(), but return every address along the way."""
    hops = []
    raw = process.read(base + rva, 8)
    if raw is None:
        return hops
    address = struct.unpack("<Q", raw)[0]
    hops.append(address)
    for offset in offsets:
        if not address:
            return hops
        raw = process.read(address + offset, 8)
        if raw is None:
            return hops
        address = struct.unpack("<Q", raw)[0]
        hops.append(address)
    return hops


def scan_object(process, address, value, span=0x3000):
    """Offsets within an object whose qword equals `value`."""
    raw = process.read(address, span)
    if raw is None:
        for smaller in (0x2000, 0x1000, 0x800):
            raw = process.read(address, smaller)
            if raw is not None:
                span = smaller
                break
    if raw is None:
        return []
    needle = struct.pack("<Q", value)
    out = []
    start = 0
    while True:
        found = raw.find(needle, start)
        if found < 0:
            break
        if found % 8 == 0:
            out.append(found)
        start = found + 1
    return out


def anatomy(process, values, world, pawn):
    """Locate everything the bridge needs, relative to the pawn and the world."""
    root = address_of(values, "root_addr")
    if root:
        hits = scan_object(process, pawn, root)
        print("RootComponent inside the pawn: %s"
              % (", ".join("+0x%X" % h for h in hits) or "not found"))

    try:
        expected = float(values.get("time_seconds", "nan"))
    except ValueError:
        expected = float("nan")
    if expected == expected and expected > 0:
        raw = process.read(world, 0x800)
        if raw is not None:
            hits = []
            for offset in range(0, len(raw) - 8 + 1, 8):
                candidate = struct.unpack_from("<d", raw, offset)[0]
                if abs(candidate - expected) < 0.25:
                    hits.append((offset, candidate))
            print("TimeSeconds inside the world (UE4SS says %.3f): %s"
                  % (expected, ", ".join("+0x%X=%.3f" % h for h in hits)
                     or "not found"))

    for key in ("controller_addr", "localplayer_addr", "camera_addr",
                "playerstate_addr", "gameinstance_addr"):
        other = address_of(values, key)
        if not other:
            continue
        holds = scan_object(process, other, pawn)
        print("%-18s 0x%X holds the pawn at %s"
              % (key, other, ", ".join("+0x%X" % h for h in holds) or "no offset"))
        back = scan_object(process, pawn, other, span=0x2000)
        if back:
            print("%-18s   ...and the pawn points back at it from %s"
                  % ("", ", ".join("+0x%X" % h for h in back)))

    for key in ("loc", "rot", "vel"):
        raw = values.get(key)
        if not raw or raw == "nil" or not root:
            continue
        wanted = [float(part) for part in raw.split(",")]
        block = process.read(root, 0x400)
        if block is None:
            continue
        hits = []
        for offset in range(0, len(block) - 24 + 1, 8):
            triple = struct.unpack_from("<3d", block, offset)
            if all(abs(a - b) < 1.0 for a, b in zip(triple, wanted)):
                hits.append(offset)
        print("%s inside the root component: %s"
              % (key, ", ".join("+0x%X (%d)" % (h, h) for h in hits)
                 or "not found"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.path.join(
        os.environ.get("TEMP", "."), "gr_target.txt"),
        help="the file UE4SS writes, used as ground truth")
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--gworld-rva", type=lambda s: int(s, 16), default=None,
                        help="skip the module scan and use this RVA")
    parser.add_argument("--verify", default=None,
                        help="routes to check, as RVA:off:off (comma-separated)")
    parser.add_argument("--goal", default="pawn_addr",
                        help="which address from the target file to search for")
    parser.add_argument("--anatomy", action="store_true",
                        help="locate the pieces the bridge needs inside the "
                             "pawn and the world")
    parser.add_argument("--watch", type=float, default=0.0,
                        help="keep re-checking for this many seconds")
    args = parser.parse_args()

    process = memory.Process(EXE)
    found = module_range(process.pid, EXE)
    if not found:
        print("could not find the game module")
        return 2
    base, size = found
    print("pid %d, module 0x%X size 0x%X" % (process.pid, base, size))

    if args.verify:
        routes = [[int(p, 16) for p in route.split(":")]
                  for route in args.verify.split(",")]
        deadline = time.time() + max(args.watch, 0)
        while True:
            values = (read_target_file(args.target)
                      if os.path.exists(args.target) else {})
            pawn = address_of(values, "pawn_addr")
            stamp = time.strftime("%H:%M:%S")
            for route in routes:
                hops = walk_verbose(process, base, route[0], route[1:])
                landed = hops[-1] if hops else None
                trail = " -> ".join("0x%X" % h for h in hops)
                verdict = "?"
                if pawn:
                    verdict = "MATCH" if landed == pawn else "MISS"
                print("%s  %-28s %s  [%s]"
                      % (stamp, describe(base, route[0], route[1:]), trail, verdict))
            if pawn:
                print("%s  UE4SS pawn 0x%X" % (stamp, pawn))
            if time.time() >= deadline:
                break
            print("")
            time.sleep(2.0)
        return 0

    values = read_target_file(args.target)
    world = address_of(values, "world_addr")
    pawn = address_of(values, "pawn_addr")
    if not world or not pawn:
        print("need world_addr and pawn_addr in %s" % args.target)
        return 2
    print("UE4SS: world 0x%X pawn 0x%X" % (world, pawn))

    if args.anatomy:
        anatomy(process, values, world, pawn)
        return 0

    started = time.time()
    space = Space(process)
    print("address space: %d regions, %.1f GB readable (%.1fs)"
          % (len(space.regions), space.total / 1e9, time.time() - started))

    if args.gworld_rva is not None:
        roots_rva = [args.gworld_rva]
    else:
        module_regions = [(a, s) for a, s in space.regions
                          if a >= base and a < base + size]
        started = time.time()
        hits = space.find_value(world, module_regions)
        print("GWorld candidates in module: %d (%.1fs)"
              % (len(hits), time.time() - started))
        roots_rva = [h - base for h in hits]
        for rva in roots_rva[:20]:
            print("  module+0x%X" % rva)
    if not roots_rva:
        print("no static holds the world pointer; nothing to walk from")
        return 1

    walker = Walker(process, space, base, size)
    started = time.time()
    goal = address_of(values, args.goal) or pawn
    print("searching for %s = 0x%X" % (args.goal, goal))
    routes = walker.search([(world, "object")], goal, max_depth=args.max_depth)
    print("search took %.1fs, %d route(s)" % (time.time() - started, len(routes)))
    if not routes:
        print("no route from the world to the pawn within depth %d" % args.max_depth)
        return 1

    print("")
    print("routes (verify each by re-running after a restart):")
    for offsets in routes[:40]:
        for rva in roots_rva[:4]:
            landed = walk(process, base, rva, offsets)
            mark = "OK " if landed == goal else "   "
            print("  %s%s" % (mark, describe(base, rva, offsets)))
    print("")
    print("re-check one with:  py -3 tools/ptrscan.py --verify %X:%s"
          % (roots_rva[0], ":".join("%X" % o for o in routes[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
