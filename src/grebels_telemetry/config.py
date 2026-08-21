"""User settings, persisted next to the user's other app data."""

import json
import os

APP_NAME = "GRebelsTelemetry"
GAME_FOLDER_NAME = "G-Rebels"
BINARIES_SUBPATH = os.path.join("G_Rebels", "Binaries", "Win64")


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def config_path():
    return os.path.join(config_dir(), "settings.json")


class Config:
    """Defaults chosen for a motion platform, not for a chart.

    fit_window_s trades cue latency against noise. Position and orientation are
    exact, but acceleration must be derived, because the game exposes no
    velocity field we can read. 0.30 s was picked by measuring a recorded
    flight: it recovers mean acceleration to within 3% of a long-window
    reference while keeping the sign-flip rate under 3 per second. Shortening
    it to 0.10 s inflates reported G roughly fivefold, all of it noise.
    """

    DEFAULTS = {
        "output_mode": "both",          # "dr2" | "simdef" | "both"
        "host": "127.0.0.1",            # DiRT Rally 2.0 compatibility output
        "port": 20777,
        "simdef_host": "127.0.0.1",     # SimHub native external-sim output
        "simdef_port": 30777,
        "send_rate_hz": 100.0,
        "poll_hz": 1000.0,
        "fit_window_s": 0.30,
        "stale_after_s": 0.30,
        "target_refresh_s": 0.5,
        "g_clamp": 6.0,
        "send_g_forces": True,
        "allow_fallback_offsets": True,
        # Find the craft by walking pointers from the executable rather than
        # by reading what an injected mod publishes. UE4SS cannot coexist with
        # a VR runtime, so this is what makes telemetry work in the headset.
        # Setting it False falls back to the UE4SS mod's file.
        "use_pointer_chain": True,
        # Synthetic engine channels. G-Rebels has no engine, but AZOM drives
        # the AB9's buzz from Rpm/MaxRpm and its shift kick from Gear, so we
        # fabricate them from thrust and speed to get haptics out of hardware
        # that only speaks car.
        "synth_engine": True,
        "synth_idle_rpm": 1200.0,
        "synth_max_rpm": 8000.0,
        "synth_reference_speed_ms": 200.0,   # fallback if the game's own max is unusable
        "synth_gear": False,                 # AZOM kicks the stick on every gear change
        "synth_gear_count": 6,
        "game_path": "",
    }

    def __init__(self, **overrides):
        for key, value in self.DEFAULTS.items():
            setattr(self, key, value)
        for key, value in overrides.items():
            if key in self.DEFAULTS:
                setattr(self, key, value)

    # -- derived ------------------------------------------------------------
    @property
    def binaries_dir(self):
        if not self.game_path:
            return ""
        return os.path.join(self.game_path, BINARIES_SUBPATH)

    @property
    def target_file(self):
        return os.path.join(self.binaries_dir, "gr_target.txt")

    # -- persistence --------------------------------------------------------
    def to_dict(self):
        return {key: getattr(self, key) for key in self.DEFAULTS}

    def save(self, path=None):
        path = path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump(self.to_dict(), handle, indent=2)

    @classmethod
    def load(cls, path=None):
        path = path or config_path()
        try:
            with open(path) as handle:
                return cls(**json.load(handle))
        except (OSError, ValueError):
            return cls()


# -------------------------------------------------------------- game hunt --
def steam_library_paths():
    """Every Steam library folder listed in libraryfolders.vdf."""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Steam"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
    ]
    libraries = []
    for steam_root in candidates:
        vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        if not os.path.exists(vdf):
            continue
        libraries.append(steam_root)
        try:
            with open(vdf, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"path"' not in line:
                        continue
                    parts = line.split('"')
                    if len(parts) >= 4:
                        libraries.append(parts[3].replace("\\\\", "\\"))
        except OSError:
            pass
    seen, unique = set(), []
    for path in libraries:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_game_path():
    """Locate the G-Rebels install folder, or "" if we cannot."""
    for library in steam_library_paths():
        candidate = os.path.join(library, "steamapps", "common", GAME_FOLDER_NAME)
        if os.path.isdir(os.path.join(candidate, BINARIES_SUBPATH)):
            return candidate
    return ""


def looks_like_game_path(path):
    return bool(path) and os.path.isfile(
        os.path.join(path, BINARIES_SUBPATH, "G_Rebels-Win64-Shipping.exe"))
