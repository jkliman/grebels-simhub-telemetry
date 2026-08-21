"""Console helper the MSI calls to finish setup.

Split out from the GUI so the installer can run it silently:

    grsetup.exe --definition --simhub "C:\\Program Files (x86)\\SimHub" --azom

Exit codes: 0 success, 1 a step failed, 2 bad arguments. Failures are reported
but never fatal to the install -- a user who ends up without AZOM should still
end up with a working telemetry app.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from grebels_telemetry import simhub_setup


def bundled_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    parser = argparse.ArgumentParser(description="G-Rebels SimHub setup")
    parser.add_argument("--definition", action="store_true",
                        help="install the G Rebels definition for SimHub")
    parser.add_argument("--azom", action="store_true",
                        help="download the latest stable AZOM and install it")
    parser.add_argument("--simhub", default="",
                        help="SimHub install folder (required for --azom)")
    args = parser.parse_args(argv)

    if not (args.definition or args.azom):
        parser.error("nothing to do: pass --definition and/or --azom")

    failures = 0

    if args.definition:
        try:
            simhub_setup.install_definition(bundled_dir())
        except Exception as exc:                       # keep going regardless
            print("Definition install failed: %s" % exc)
            failures += 1

    if args.azom:
        simhub = args.simhub or simhub_setup.find_simhub_dir()
        if not simhub:
            print("AZOM skipped: SimHub was not found. Install it from %s"
                  % simhub_setup.SIMHUB_DOWNLOAD_URL)
            failures += 1
        else:
            try:
                simhub_setup.install_azom(simhub)
            except Exception as exc:
                print("AZOM install failed: %s" % exc)
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
