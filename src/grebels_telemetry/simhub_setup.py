"""Setting up the SimHub side: the game definition, and optionally AZOM.

Kept out of the MSI deliberately. Downloading a file over HTTP, unpacking it and
writing into Program Files is miserable to express as MSI custom actions and
worse to debug; here it is ordinary code that can be tested directly. The
installer collects a folder and a checkbox and calls this.

On AZOM: it is GPL-3.0 and this project is MIT, so its DLL is never bundled.
It is fetched from the official repository at install time, which leaves the
user obtaining the binary from upstream and us merely placing it.
"""

import io
import json
import os
import shutil
import ssl
import urllib.request
import zipfile

AZOM_REPO = "giantorth/AZOM"
AZOM_LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % AZOM_REPO
AZOM_PLUGIN_NAME = "MozaPlugin.dll"
SIMHUB_DOWNLOAD_URL = "https://www.simhubdash.com/download-2/?downloadnow=1"
SIMHUB_PROCESS = "SimHubWPF.exe"

USER_AGENT = "grebels-simhub-telemetry"
#: A plugin DLL well under this is a broken download, well over is not a plugin.
MIN_PLUGIN_BYTES = 256 * 1024
MAX_PLUGIN_BYTES = 64 * 1024 * 1024


class SetupError(RuntimeError):
    pass


# ------------------------------------------------------------- locating it --
def candidate_simhub_dirs():
    seen, out = set(), []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if root:
            path = os.path.join(root, "SimHub")
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def looks_like_simhub(path):
    """SimHub is identified by its executable, not by the folder name."""
    return bool(path) and os.path.isfile(os.path.join(path, SIMHUB_PROCESS))


def find_simhub_dir():
    for path in candidate_simhub_dirs():
        if looks_like_simhub(path):
            return path
    return ""


def definitions_dir():
    """Where SimHub reads external sim definitions.

    Note this is per-user app data, NOT the SimHub program folder -- so the
    definition install needs no elevation and no browse dialog. Only the AZOM
    plugin actually lands in Program Files.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SetupError("LOCALAPPDATA is not set")
    return os.path.join(local, "SimHub", "ExternalSims", "Definitions")


def simhub_is_running():
    """AZOM's DLL cannot be replaced while SimHub holds it open."""
    try:
        import ctypes
        from ctypes import wintypes as W
    except ImportError:
        return False

    TH32CS_SNAPPROCESS = 0x0002

    class _PE(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                    ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                    ("th32ParentProcessID", W.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return False
    entry = _PE()
    entry.dwSize = ctypes.sizeof(_PE)
    found = False
    try:
        if k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode(errors="ignore").lower()
                if name == SIMHUB_PROCESS.lower():
                    found = True
                    break
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return found


# ------------------------------------------------------- the definition ----
def install_definition(bundled_dir, progress=print):
    """Copy the .simdef and its artwork into SimHub's Definitions folder.

    They must travel together: IconPath inside the definition is relative.
    """
    source = os.path.join(bundled_dir, "simhub")
    if not os.path.isdir(source):
        raise SetupError("the bundled definition is missing from %s" % bundled_dir)

    destination = definitions_dir()
    os.makedirs(destination, exist_ok=True)

    wanted = (".simdef", ".png", ".jpg", ".jpeg")
    copied = []
    for name in sorted(os.listdir(source)):
        origin = os.path.join(source, name)
        if not os.path.isfile(origin):
            continue
        if os.path.splitext(name)[1].lower() not in wanted:
            continue          # our own README has no business in SimHub's data
        shutil.copy2(origin, os.path.join(destination, name))
        copied.append(name)

    if not any(n.lower().endswith(".simdef") for n in copied):
        raise SetupError("no .simdef found to install")
    progress("Installed the G Rebels definition: %s" % ", ".join(copied))
    return destination


# -------------------------------------------------------------- AZOM ------
def latest_azom_release(timeout=30):
    """The newest STABLE AZOM release.

    /releases/latest is used rather than the top of /releases on purpose: that
    list is dominated by PR prereleases -- three of the four most recent
    entries at time of writing -- and installing one of those on a user's rig
    would be reckless. This endpoint excludes prereleases by definition.
    """
    request = urllib.request.Request(
        AZOM_LATEST_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        payload = json.load(response)

    if payload.get("prerelease"):
        raise SetupError("GitHub returned a prerelease as latest; refusing")

    for asset in payload.get("assets", []):
        if asset.get("name", "").lower().endswith(".zip"):
            return {"tag": payload.get("tag_name", "?"),
                    "name": asset["name"],
                    "url": asset["browser_download_url"],
                    "size": asset.get("size", 0)}
    raise SetupError("no .zip asset on AZOM release %s" % payload.get("tag_name"))


def extract_plugin(archive_bytes):
    """Pull the plugin DLL out of the release archive, safely.

    Guards against a zip whose entry names contain paths -- the archive should
    hold exactly one bare DLL, and anything else is either a changed release
    format or something hostile. Either way it should not be written blind.
    """
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".dll")]
        if not names:
            raise SetupError("no DLL inside the AZOM archive")
        if len(names) > 1:
            raise SetupError("expected one DLL in the AZOM archive, found %d"
                             % len(names))
        name = names[0]
        if os.path.basename(name) != name or os.path.isabs(name) or ".." in name:
            raise SetupError("refusing archive entry with a path: %r" % name)
        data = archive.read(name)

    if not (MIN_PLUGIN_BYTES <= len(data) <= MAX_PLUGIN_BYTES):
        raise SetupError("plugin DLL is an implausible size (%d bytes)" % len(data))
    if data[:2] != b"MZ":
        raise SetupError("plugin DLL is not a Windows binary")
    return os.path.basename(name), data


def install_azom(simhub_dir, progress=print, timeout=180):
    """Fetch the latest stable AZOM and place its plugin in SimHub."""
    if not looks_like_simhub(simhub_dir):
        raise SetupError("%s does not look like a SimHub install (no %s)"
                         % (simhub_dir, SIMHUB_PROCESS))
    if simhub_is_running():
        raise SetupError("SimHub is running and holds the plugin open. "
                         "Close SimHub and try again.")

    release = latest_azom_release()
    progress("Downloading AZOM %s (%s)..." % (release["tag"], release["name"]))
    request = urllib.request.Request(release["url"],
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout,
                                context=ssl.create_default_context()) as response:
        payload = response.read()

    name, data = extract_plugin(payload)
    target = os.path.join(simhub_dir, name)

    # Keep one backup. If the new build misbehaves there is a way back that
    # does not involve hunting for the old release.
    if os.path.exists(target):
        backup = target + ".bak"
        shutil.copy2(target, backup)
        progress("Backed up the existing plugin to %s" % os.path.basename(backup))

    with open(target, "wb") as handle:
        handle.write(data)
    progress("Installed %s (%s, %.1f MB). SimHub will ask you to enable it on "
             "next start." % (name, release["tag"], len(data) / 1048576.0))
    return target
