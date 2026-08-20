"""Read-only access to another process's memory (Windows)."""

import ctypes
import struct
from ctypes import wintypes as W

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x0002
TH32CS_SNAPMODULE = 0x0008 | 0x0010


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", W.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("th32ModuleID", W.DWORD), ("th32ProcessID", W.DWORD),
        ("GlblcntUsage", W.DWORD), ("ProccntUsage", W.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", W.DWORD),
        ("hModule", W.HMODULE), ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]


class ProcessNotRunning(RuntimeError):
    pass


def find_pid(exe_name):
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return None
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    pid = None
    try:
        if k32.Process32First(snap, ctypes.byref(entry)):
            target = exe_name.lower()
            while True:
                if entry.szExeFile.decode(errors="ignore").lower() == target:
                    pid = entry.th32ProcessID
                    break
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return pid


class Process:
    """A handle onto a running process, opened for reading only.

    Deliberately never asks for write access: this tool observes a game, it
    does not modify one, and the narrower handle is both safer and less likely
    to upset anti-tamper heuristics.
    """

    def __init__(self, exe_name):
        self.exe_name = exe_name
        self.k32 = ctypes.windll.kernel32
        self.pid = find_pid(exe_name)
        if self.pid is None:
            raise ProcessNotRunning(exe_name + " is not running")
        self.handle = self.k32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, self.pid)
        if not self.handle:
            raise RuntimeError(
                "OpenProcess failed for %s (error %d)"
                % (exe_name, ctypes.GetLastError()))
        self._buffers = {}

    def close(self):
        if getattr(self, "handle", None):
            self.k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, address, size):
        """Return `size` bytes at `address`, or None if unreadable.

        Buffers are cached per size: this runs a thousand times a second and
        allocating a fresh ctypes buffer each call is pure garbage pressure.
        """
        buf = self._buffers.get(size)
        if buf is None:
            buf = ctypes.create_string_buffer(size)
            self._buffers[size] = buf
        got = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got))
        if not ok or got.value != size:
            return None
        return buf.raw

    def read_doubles(self, address, count):
        raw = self.read(address, 8 * count)
        if raw is None:
            return None
        return struct.unpack("<%dd" % count, raw)

    def read_double(self, address):
        raw = self.read(address, 8)
        if raw is None:
            return None
        return struct.unpack("<d", raw)[0]

    def is_alive(self):
        return find_pid(self.exe_name) == self.pid

    def module_base(self, module_name=None):
        module_name = (module_name or self.exe_name).lower()
        k32 = self.k32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, self.pid)
        if snap == -1:
            return None
        entry = _ModuleEntry()
        entry.dwSize = ctypes.sizeof(_ModuleEntry)
        base = None
        try:
            if k32.Module32First(snap, ctypes.byref(entry)):
                while True:
                    if entry.szModule.decode(errors="ignore").lower() == module_name:
                        base = entry.modBaseAddr
                        break
                    if not k32.Module32Next(snap, ctypes.byref(entry)):
                        break
        finally:
            k32.CloseHandle(snap)
        return base
