"""Run the bridge with no window, and say what it is doing.

For proving the pointer chain works with UE4SS removed: there is no UI on a
machine wearing a headset, so the bridge reports to a console instead.

    py -3 tools/headless.py --seconds 120
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from grebels_telemetry import bridge as bridge_module  # noqa: E402
from grebels_telemetry.config import Config  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--game-path", default="")
    parser.add_argument("--no-chain", action="store_true",
                        help="ignore the pointer chain and use the UE4SS mod")
    args = parser.parse_args()

    config = Config.load()
    if args.game_path:
        config.game_path = args.game_path
    if args.no_chain:
        config.use_pointer_chain = False
    print("game path: %s" % (config.game_path or "(unset)"))
    print("pointer chain: %s" % config.use_pointer_chain)
    print("sending to %s:%d (simdef) and %s:%d (dr2), mode %s"
          % (config.simdef_host, config.simdef_port,
             config.host, config.port, config.output_mode))

    engine = bridge_module.Bridge(config)
    engine.start()
    deadline = time.time() + args.seconds
    last = ""
    try:
        while time.time() < deadline:
            snap = engine.status.snapshot()
            line = ("%-14s src=%-14s craft=%-22s pkts=%-7d %.1f/s  "
                    "speed=%6.1f m/s  alt=%8.1f m  gx=%5.2f gy=%5.2f  %s"
                    % (snap["state"], snap.get("source", "?"),
                       (snap["craft"] or "-")[:22], snap["packets_sent"],
                       snap["packet_rate"], snap["speed_ms"], snap["altitude_m"],
                       snap["g_longitudinal"], snap["g_lateral"],
                       snap["detail"][:40]))
            if line != last:
                print(time.strftime("%H:%M:%S "), line)
                last = line
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
