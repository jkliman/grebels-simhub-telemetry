"""Pull the useful facts out of a Windows minidump, without a debugger.

    python tools\\parse_minidump.py crash.dmp

Answers the only question that matters when a game starts crashing after you
installed something: which module was executing when it died. A full debugger
would tell you more, but this needs nothing installed and reads only a few
kilobytes of a file that is usually tens of megabytes.

Minidump layout, for anyone maintaining this: a header points at a stream
directory; each entry gives a type, a size and an offset. We want the module
list (type 4), the exception record (type 6) and the system info (type 7).
"""

import datetime
import struct
import sys

STREAM_MODULE_LIST = 4
STREAM_EXCEPTION = 6
STREAM_SYSTEM_INFO = 7

#: The handful of exception codes worth naming; the rest print as hex.
EXCEPTION_NAMES = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000096: "PRIVILEGED_INSTRUCTION",
    0xC0000374: "HEAP_CORRUPTION",
    0x80000003: "BREAKPOINT",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xE0434352: "CLR_EXCEPTION",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000139: "ENTRYPOINT_NOT_FOUND",
}

ARCHITECTURES = {0: "x86", 5: "ARM", 6: "IA64", 9: "x64", 12: "ARM64"}


def read_string(data, rva):
    """MINIDUMP_STRING: byte length, then UTF-16LE, no terminator counted."""
    if rva == 0 or rva + 4 > len(data):
        return ""
    length = struct.unpack_from("<I", data, rva)[0]
    raw = data[rva + 4:rva + 4 + length]
    return raw.decode("utf-16-le", "replace")


def parse(path):
    with open(path, "rb") as handle:
        data = handle.read()

    if data[:4] != b"MDMP":
        raise SystemExit(
            "not a minidump: starts with %r.\n"
            "If this came from a browser download it is probably an HTML "
            "sign-in page rather than the file." % data[:8])

    stream_count, directory_rva = struct.unpack_from("<II", data, 8)
    streams = {}
    for index in range(stream_count):
        offset = directory_rva + index * 12
        kind, size, rva = struct.unpack_from("<III", data, offset)
        streams[kind] = (size, rva)

    # -- system info --------------------------------------------------------
    if STREAM_SYSTEM_INFO in streams:
        _, rva = streams[STREAM_SYSTEM_INFO]
        arch, _level, _rev, _cpus, _type = struct.unpack_from("<HHHBB", data, rva)
        major, minor, build = struct.unpack_from("<III", data, rva + 8)
        print("system: %s, Windows %d.%d build %d"
              % (ARCHITECTURES.get(arch, "arch %d" % arch), major, minor, build))

    # -- modules ------------------------------------------------------------
    modules = []
    if STREAM_MODULE_LIST in streams:
        _, rva = streams[STREAM_MODULE_LIST]
        count = struct.unpack_from("<I", data, rva)[0]
        for index in range(count):
            entry = rva + 4 + index * 108
            base, size, _sum, stamp, name_rva = struct.unpack_from(
                "<QIIII", data, entry)
            # VS_FIXEDFILEINFO starts 24 bytes in; file version is at +8.
            ms, ls = struct.unpack_from("<II", data, entry + 24 + 8)
            version = "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF,
                                       ls >> 16, ls & 0xFFFF)
            modules.append({
                "base": base, "size": size, "name": read_string(data, name_rva),
                "version": version, "stamp": stamp,
            })
    print("modules loaded: %d" % len(modules))

    def owner_of(address):
        for module in modules:
            if module["base"] <= address < module["base"] + module["size"]:
                return module
        return None

    # -- the exception ------------------------------------------------------
    if STREAM_EXCEPTION not in streams:
        print("\nno exception stream: this dump was not written by a crash "
              "(it may be a hang or a manual dump)")
        return modules

    _, rva = streams[STREAM_EXCEPTION]
    thread_id = struct.unpack_from("<I", data, rva)[0]
    code, flags = struct.unpack_from("<II", data, rva + 8)
    address = struct.unpack_from("<Q", data, rva + 24)[0]
    param_count = struct.unpack_from("<I", data, rva + 32)[0]
    params = struct.unpack_from("<%dQ" % min(param_count, 15), data, rva + 40)

    print("\nEXCEPTION")
    print("  code    : 0x%08X  %s"
          % (code, EXCEPTION_NAMES.get(code, "(unnamed)")))
    print("  thread  : %d   flags: 0x%X" % (thread_id, flags))
    print("  address : 0x%016X" % address)

    if code == 0xC0000005 and param_count >= 2:
        kind = {0: "reading", 1: "writing", 8: "executing"}.get(
            params[0], "accessing")
        print("  fault   : %s 0x%016X" % (kind, params[1]))
        if params[1] < 0x10000:
            print("            (a null-pointer dereference)")

    culprit = owner_of(address)
    print("\nFAULTING MODULE")
    if culprit:
        print("  %s" % culprit["name"])
        print("  version %s, loaded at 0x%X, offset +0x%X"
              % (culprit["version"], culprit["base"], address - culprit["base"]))
    else:
        print("  none: 0x%016X is not inside any loaded module." % address)
        print("  That usually means a corrupt call, JIT-ed code, or a stack")
        print("  smashed badly enough that the return address is garbage.")

    # -- anything we installed ---------------------------------------------
    interesting = ("ue4ss", "dwmapi", "mozaplugin", "direct_force")
    hits = [m for m in modules
            if any(word in m["name"].lower() for word in interesting)]
    print("\nMODULES WE CARE ABOUT")
    if hits:
        for module in hits:
            when = datetime.datetime.utcfromtimestamp(module["stamp"]) \
                if 0 < module["stamp"] < 2 ** 31 else None
            print("  %-58s v%s%s" % (module["name"], module["version"],
                                     "  built %s" % when.date() if when else ""))
    else:
        print("  none loaded -- UE4SS was not in the process when it died")
    return modules


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    parse(sys.argv[1])
