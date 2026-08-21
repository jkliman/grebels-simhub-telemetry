"""Console helper the MSI calls to finish setup.

Split out from the GUI so the installer can run it silently:

    grsetup.exe --definition --azom --simhub "C:\\...\\SimHub"
    grsetup.exe --check          # what actually got installed, and where

Exit codes: 0 success, 1 a step failed, 2 bad arguments. Failures are reported
but never fatal to the install -- a user who ends up without AZOM should still
end up with a working telemetry app.

Everything is also written to %TEMP%\\grsetup.log. The MSI runs this with its
output discarded, so without a log a failure is completely invisible: the
install reports success and the plugin simply is not there. That is precisely
what happened on the first real install, where SimHub was open and holding the
DLL, and nothing surfaced the refusal.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from grebels_telemetry import config, installer, simhub_setup


LOG_NAME = "grsetup.log"


def log_path():
    return os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), LOG_NAME)


class Reporter:
    """Prints and logs. Deliberately never raises: a logging problem must not
    become the reason setup failed."""

    def __init__(self):
        self.handle = None
        try:
            self.handle = open(log_path(), "a", encoding="utf-8")
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.handle.write("\n=== %s  %s\n" % (stamp, " ".join(sys.argv[1:])))
        except OSError:
            pass

    def __call__(self, message):
        print(message)
        if self.handle:
            try:
                self.handle.write(message + "\n")
                self.handle.flush()
            except OSError:
                pass

    def close(self):
        if self.handle:
            try:
                self.handle.close()
            except OSError:
                pass


def check(report):
    """Report what is installed, so a silent failure can be diagnosed later."""
    definitions = simhub_setup.definitions_dir()
    simdef = os.path.join(definitions, "G Rebels.simdef")
    report("definition folder: %s" % definitions)
    report("  G Rebels.simdef: %s" % ("present" if os.path.isfile(simdef) else "MISSING"))

    game = config.find_game_path()
    report("G-Rebels: %s" % (game or "NOT FOUND"))
    if game:
        binaries = os.path.join(game, config.BINARIES_SUBPATH)
        installed = installer.is_installed(binaries)
        report("  UE4SS fallback: %s"
               % ("installed - remove it if you fly in VR" if installed
                  else "not installed (correct: telemetry does not need it)"))

    simhub = simhub_setup.find_simhub_dir()
    report("SimHub: %s" % (simhub or "NOT FOUND"))
    report("SimHub running: %s" % ("yes (blocks the AZOM install)"
                                   if simhub_setup.simhub_is_running() else "no"))
    if simhub:
        plugin = os.path.join(simhub, simhub_setup.AZOM_PLUGIN_NAME)
        if os.path.isfile(plugin):
            size = os.path.getsize(plugin)
            when = datetime.datetime.fromtimestamp(os.path.getmtime(plugin))
            report("  %s: present, %.1f MB, modified %s"
                   % (simhub_setup.AZOM_PLUGIN_NAME, size / 1048576.0, when))
        else:
            report("  %s: MISSING" % simhub_setup.AZOM_PLUGIN_NAME)
    return 0


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
    parser.add_argument("--ue4ss", "--game", action="store_true", dest="ue4ss",
                        help="install the optional UE4SS fallback into G-Rebels. "
                             "Not needed for telemetry, and it prevents the game "
                             "from launching in VR")
    parser.add_argument("--game-dir", default="",
                        help="G-Rebels folder (found automatically if omitted)")
    parser.add_argument("--check", action="store_true",
                        help="report what is installed and exit")
    args = parser.parse_args(argv)

    if not (args.definition or args.azom or args.ue4ss or args.check):
        parser.error("nothing to do: pass --definition, --azom, --ue4ss or --check")

    report = Reporter()
    failures = 0
    try:
        if args.check:
            return check(report)

        if args.ue4ss:
            # Discovery beats the registry here: Steam writes an uninstall key
            # for the game but leaves InstallLocation empty, so the library has
            # to be found by parsing libraryfolders.vdf, which is what
            # find_game_path does.
            game = args.game_dir or config.find_game_path()
            if not config.looks_like_game_path(game):
                report("Game setup SKIPPED: G-Rebels not found%s. Run the app "
                       "and use Install / repair once the game is installed."
                       % (" at " + game if game else ""))
                failures += 1
            else:
                report("Setting up %s" % game)
                try:
                    installer.install(
                        os.path.join(game, config.BINARIES_SUBPATH),
                        bundled_dir(), report)
                except Exception as exc:
                    report("Game setup FAILED: %s" % exc)
                    failures += 1

        if args.definition:
            try:
                simhub_setup.install_definition(bundled_dir(), report)
            except Exception as exc:                   # keep going regardless
                report("Definition install FAILED: %s" % exc)
                failures += 1

        if args.azom:
            simhub = args.simhub or simhub_setup.find_simhub_dir()
            if not simhub:
                report("AZOM SKIPPED: SimHub was not found. Install it from %s"
                       % simhub_setup.SIMHUB_DOWNLOAD_URL)
                failures += 1
            else:
                try:
                    simhub_setup.install_azom(simhub, report)
                except Exception as exc:
                    report("AZOM install FAILED: %s" % exc)
                    failures += 1

        report("finished with %d failure(s); log at %s" % (failures, log_path()))
    finally:
        report.close()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
