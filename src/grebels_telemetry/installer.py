"""One-click setup: put UE4SS and the telemetry mod into the game folder.

G-Rebels ships on Unreal Engine 5.8, which UE4SS does not officially support
(5.7 is its ceiling). Three things make it work anyway, and this module applies
all of them:

1. Current UE4SS binaries. The tagged releases predate 5.8 by a long way, so we
   pull the artifact from the latest successful experimental-release run.
2. An engine version override pinning UE4SS to 5.7 behaviour.
3. Signature and layout overrides for the three AOB scans that fail on 5.8
   (the FName constructor, FUObjectHashTables::Get and GNatives), plus a
   GUObjectArray override -- without that last one UE4SS locks onto an adjacent
   global that reads zero objects and hangs on "Waiting for object
   construction" forever, with no error.

Nothing here touches the game's own files; everything is added alongside them
and can be removed again by uninstall().
"""

import io
import json
import os
import shutil
import urllib.request
import zipfile

UE4SS_REPO = "UE4SS-RE/RE-UE4SS"
WORKFLOW_NAME = "Make Experimental Release"
SOURCE_ZIP = "https://github.com/%s/archive/refs/heads/main.zip" % UE4SS_REPO
NIGHTLY_TEMPLATE = "https://nightly.link/%s/actions/runs/%s/%s.zip"
USER_AGENT = "grebels-simhub-telemetry"

REQUIRED_BINARIES = ("dwmapi.dll", "UE4SS.dll")
INSTALLED_MARKER = "grebels-telemetry-install.json"


from . import simdef


class InstallError(RuntimeError):
    pass


def _get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _get_json(url):
    return json.loads(_get(url).decode("utf-8"))


# ------------------------------------------------------------- ue4ss fetch --
def latest_experimental_run():
    """Run id + artifact name of the most recent successful UE4SS build."""
    url = ("https://api.github.com/repos/%s/actions/runs"
           "?status=success&per_page=20&branch=main" % UE4SS_REPO)
    runs = _get_json(url).get("workflow_runs", [])
    for run in runs:
        if run.get("name") != WORKFLOW_NAME:
            continue
        artifacts = _get_json(run["artifacts_url"]).get("artifacts", [])
        for artifact in artifacts:
            if not artifact.get("expired"):
                return str(run["id"]), artifact["name"]
    raise InstallError("no current UE4SS build found on GitHub")


def download_ue4ss_binaries(progress=print):
    progress("Finding the latest UE4SS build...")
    run_id, artifact_name = latest_experimental_run()
    url = NIGHTLY_TEMPLATE % (UE4SS_REPO, run_id, artifact_name)
    progress("Downloading UE4SS (run %s)..." % run_id)
    return zipfile.ZipFile(io.BytesIO(_get(url, timeout=180)))


def download_ue4ss_assets(progress=print):
    """UE4SS's own settings file and stock mods, from the repo source."""
    progress("Downloading UE4SS support files...")
    return zipfile.ZipFile(io.BytesIO(_get(SOURCE_ZIP, timeout=180)))


# ------------------------------------------------------------------ install --
def _extract_named(archive, filename, destination):
    for member in archive.namelist():
        if member.endswith("/"):
            continue
        if os.path.basename(member) == filename:
            with archive.open(member) as source, open(destination, "wb") as target:
                shutil.copyfileobj(source, target)
            return True
    return False


def _extract_tree(archive, prefix, destination):
    extracted = 0
    for member in archive.namelist():
        if member.endswith("/") or prefix not in member:
            continue
        relative = member.split(prefix, 1)[1].lstrip("/")
        if not relative:
            continue
        out_path = os.path.join(destination, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with archive.open(member) as source, open(out_path, "wb") as target:
            shutil.copyfileobj(source, target)
        extracted += 1
    return extracted


def set_engine_version_override(ini_path, major=5, minor=7):
    """Pin UE4SS to 5.7 behaviour. It has no 5.8 profile of its own."""
    with open(ini_path, encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped.lower() == "[engineversionoverride]"
            continue
        if not in_section:
            continue
        if stripped.lower().startswith("majorversion"):
            lines[index] = "MajorVersion = %d\n" % major
        elif stripped.lower().startswith("minorversion"):
            lines[index] = "MinorVersion = %d\n" % minor
    with open(ini_path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)


def install(binaries_dir, bundled_dir, progress=print):
    """Install everything into <game>/G_Rebels/Binaries/Win64."""
    if not os.path.isdir(binaries_dir):
        raise InstallError("game folder not found: %s" % binaries_dir)

    os.makedirs(binaries_dir, exist_ok=True)

    binaries = download_ue4ss_binaries(progress)
    progress("Installing UE4SS...")
    for name in REQUIRED_BINARIES:
        if not _extract_named(binaries, name, os.path.join(binaries_dir, name)):
            raise InstallError("%s missing from the UE4SS build" % name)

    assets = download_ue4ss_assets(progress)
    if not _extract_named(assets, "UE4SS-settings.ini",
                          os.path.join(binaries_dir, "UE4SS-settings.ini")):
        raise InstallError("UE4SS-settings.ini missing from the UE4SS source")
    progress("Installing UE4SS stock mods...")
    if not _extract_tree(assets, "/assets/Mods", os.path.join(binaries_dir, "Mods")):
        raise InstallError("could not extract UE4SS mods folder")

    progress("Applying UE 5.8 compatibility files...")
    set_engine_version_override(os.path.join(binaries_dir, "UE4SS-settings.ini"))
    ue58_dir = os.path.join(bundled_dir, "ue4ss-5.8")
    if not os.path.isdir(ue58_dir):
        raise InstallError("bundled UE 5.8 compatibility files are missing")
    for root, _dirs, files in os.walk(ue58_dir):
        relative = os.path.relpath(root, ue58_dir)
        out_dir = binaries_dir if relative == "." else os.path.join(binaries_dir, relative)
        os.makedirs(out_dir, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(out_dir, name))

    progress("Installing the telemetry mod...")
    mod_source = os.path.join(bundled_dir, "mod", "GRTelemetry")
    if not os.path.isdir(mod_source):
        raise InstallError("bundled telemetry mod is missing")
    mod_target = os.path.join(binaries_dir, "Mods", "GRTelemetry")
    if os.path.isdir(mod_target):
        shutil.rmtree(mod_target)
    shutil.copytree(mod_source, mod_target)

    # Bake the absolute output path into the mod. The game's working directory
    # is not reliably the Binaries folder, so a relative path would strand the
    # snapshot file somewhere the bridge does not look.
    script_path = os.path.join(mod_target, "Scripts", "main.lua")
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()
    target_file = os.path.join(binaries_dir, "gr_target.txt").replace("\\", "/")
    script = script.replace("@@OUTPUT_PATH@@", target_file)
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write(script)

    install_definition(bundled_dir, progress)

    with open(os.path.join(binaries_dir, INSTALLED_MARKER), "w") as handle:
        json.dump({"installed_by": USER_AGENT}, handle)

    progress("Setup complete. Start G-Rebels and load into a flight.")
    return True


# ------------------------------------------------------- SimHub definition --
def simhub_definitions_dir():
    """Where SimHub scans for external sim definitions, or "" if unknown."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return ""
    return os.path.join(local, "SimHub", "ExternalSims", "Definitions")


def install_definition(bundled_dir, progress=print):
    """Drop the .simdef and its icon where SimHub will find them.

    SimHub reads definitions from its OWN machine, so this only does anything
    when SimHub is installed here. On a split setup -- game on one PC, rig on
    another -- the same two files must be copied to the same folder on the
    machine actually running SimHub. Nothing else transfers; the definition is
    self-contained once the icon sits beside it.

    Drop-in rather than a .simlink registration: both are supported, but having
    a definition present twice under one UniqueId invites a conflict, and the
    drop-in needs no second file to keep in sync.
    """
    source = os.path.join(bundled_dir, "simhub")
    if not os.path.isdir(source):
        return False

    destination = simhub_definitions_dir()
    if not destination or not os.path.isdir(os.path.dirname(destination)):
        progress("SimHub not found on this PC - copy the 'simhub' folder's "
                 "contents to %%LocalAppData%%\\SimHub\\ExternalSims\\Definitions "
                 "on the machine running SimHub.")
        return False

    os.makedirs(destination, exist_ok=True)
    copied = 0
    for name in os.listdir(source):
        origin = os.path.join(source, name)
        if os.path.isfile(origin):
            shutil.copy2(origin, os.path.join(destination, name))
            copied += 1

    # A stale registration pointing at an older copy would give SimHub two
    # definitions sharing one UniqueId.
    stale = os.path.join(os.path.dirname(destination), "Registrations",
                         simdef.DEFINITION_UNIQUE_ID + ".simlink")
    if os.path.exists(stale):
        try:
            os.remove(stale)
        except OSError:
            pass

    progress("Installed the SimHub definition (%d files). Restart SimHub to "
             "see G Rebels in its game list." % copied)
    return True


def is_installed(binaries_dir):
    if not binaries_dir or not os.path.isdir(binaries_dir):
        return False
    needed = [os.path.join(binaries_dir, name) for name in REQUIRED_BINARIES]
    needed.append(os.path.join(binaries_dir, "UE4SS-settings.ini"))
    needed.append(os.path.join(binaries_dir, "Mods", "GRTelemetry", "Scripts", "main.lua"))
    needed.append(os.path.join(binaries_dir, "UE4SS_Signatures", "GUObjectArray.lua"))
    return all(os.path.exists(path) for path in needed)


def describe_install(binaries_dir):
    """What is present and what is missing, for the UI."""
    if not binaries_dir:
        return ["Game folder not set"]
    checks = [
        ("UE4SS loader (dwmapi.dll)", os.path.join(binaries_dir, "dwmapi.dll")),
        ("UE4SS (UE4SS.dll)", os.path.join(binaries_dir, "UE4SS.dll")),
        ("UE4SS settings", os.path.join(binaries_dir, "UE4SS-settings.ini")),
        ("UE 5.8 signatures",
         os.path.join(binaries_dir, "UE4SS_Signatures", "GUObjectArray.lua")),
        ("Telemetry mod",
         os.path.join(binaries_dir, "Mods", "GRTelemetry", "Scripts", "main.lua")),
    ]
    return ["%s %s" % ("[ok]  " if os.path.exists(path) else "[--]  ", label)
            for label, path in checks]


def uninstall(binaries_dir, progress=print):
    """Remove what we added. Leaves the game's own files untouched."""
    removed = []
    for name in REQUIRED_BINARIES + ("UE4SS-settings.ini", "UE4SS.log",
                                     "gr_target.txt", INSTALLED_MARKER,
                                     "MemberVariableLayout.ini", "VTableLayout.ini"):
        path = os.path.join(binaries_dir, name)
        if os.path.exists(path):
            os.remove(path)
            removed.append(name)
    for folder in ("Mods", "UE4SS_Signatures"):
        path = os.path.join(binaries_dir, folder)
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(folder + os.sep)
    progress("Removed: %s" % (", ".join(removed) if removed else "nothing"))
    return removed
