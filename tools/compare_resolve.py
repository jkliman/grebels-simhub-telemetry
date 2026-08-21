"""Check the pointer-chain resolver against UE4SS, live, for as long as you fly.

Run this with UE4SS still installed and the game running. It asks both sources
the same question -- where is the craft? -- several times a second and reports
every disagreement. Fly, get shot down, respawn, change map: anything that
makes the two answers diverge is a route that would have failed silently once
UE4SS is gone.

    py -3 tools/compare_resolve.py --seconds 600
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from grebels_telemetry import bridge, memory, resolve  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.path.join(
        "E:/SteamLibrary/steamapps/common/G-Rebels/G_Rebels/Binaries/Win64",
        "gr_target.txt"))
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--rate", type=float, default=4.0)
    args = parser.parse_args()

    process = memory.Process(bridge.GAME_PROCESS)
    resolver = resolve.Resolver(process)
    print("module 0x%X size 0x%X, GWorld at +0x%X"
          % (resolver.module_base, resolver.module_size, resolver.gworld_rva))

    agree = disagree = unresolved = no_oracle = 0
    last_line = ""
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        try:
            truth = bridge.read_target_file(args.target)
        except OSError:
            truth = None

        try:
            mine = resolver.snapshot()
        except resolve.ResolveError as exc:
            mine = None
            reason = str(exc)

        if truth is None or not truth.get("pawn_addr"):
            no_oracle += 1
        elif mine is None:
            unresolved += 1
            line = "UNRESOLVED while UE4SS has 0x%X (%s)" % (truth["pawn_addr"], reason)
            if line != last_line:
                print(time.strftime("%H:%M:%S "), line)
                last_line = line
        elif mine["pawn_addr"] == truth["pawn_addr"]:
            agree += 1
            if mine["root_addr"] != truth["root_addr"]:
                print(time.strftime("%H:%M:%S "),
                      "same pawn, different root: mine 0x%X theirs 0x%X"
                      % (mine["root_addr"], truth["root_addr"]))
        else:
            disagree += 1
            print(time.strftime("%H:%M:%S "),
                  "DISAGREE mine 0x%X theirs 0x%X  votes %s"
                  % (mine["pawn_addr"], truth["pawn_addr"],
                     ", ".join("0x%X:%d" % v for v in resolver.last_votes)))

        total = agree + disagree + unresolved + no_oracle
        if total % 20 == 0:
            clock = mine["time_seconds"] if mine else -1
            sys.stdout.write("\r  agree %d  disagree %d  unresolved %d  "
                             "no-oracle %d  clock %.1f   "
                             % (agree, disagree, unresolved, no_oracle, clock))
            sys.stdout.flush()
        time.sleep(1.0 / args.rate)

    print("")
    print("agree %d, disagree %d, unresolved %d, oracle silent %d"
          % (agree, disagree, unresolved, no_oracle))
    return 0 if disagree == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
