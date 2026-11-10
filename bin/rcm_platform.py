#!/usr/bin/env python3
"""The OS seam: everything rcm asks of the operating system, in one module.

LinuxX11 is the reference implementation — the exact behaviour the manager
has always had, moved here verbatim. WindowsNative is the port's landing
strip: the pieces that could be written and parse-tested now are real
(detached spawn, process listing, ARP lookup, opening files, credentials
through the `keyring` package, and typing them into a prompt through
user32), and everything still owned by later phases of the port plan
raises PlatformError naming that plan, so nothing degrades silently.

`PLATFORM` is chosen once from sys.platform; the rest of the code imports it
and never asks "which OS?" again. The output parsers are module-level pure
functions so the Windows ones can be tested from any machine.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


class PlatformError(RuntimeError):
    """This operation is not available on this platform (yet). Says why."""


# --------------------------------------------------------------------------- #
# programs we start, from the Windows .exe
# --------------------------------------------------------------------------- #
# The .exe (PyInstaller) points GTK at its own runtime folder through
# environment variables, and puts that folder on the DLL search path; both are
# inherited by every program it starts. A client that loads zlib1.dll or
# OpenSSL by name would then find GTK's copies before its own, and a GTK or
# Python program would look for its data in our folder. Children get neither.
FROZEN_BUNDLE = getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else ""


def child_environ(environ: dict, bundle: str) -> dict:
    """`environ` without what a frozen app at `bundle` set for itself.

    Drops PyInstaller's own `_PYI_*` markers and every variable pointing into
    the bundle (GI_TYPELIB_PATH, GTK_*, XDG_DATA_DIRS…); PATH loses only its
    entries inside the bundle. Not frozen (`bundle` empty): unchanged.
    """
    if not bundle:
        return dict(environ)
    root = os.path.normcase(os.path.normpath(bundle))

    def inside(path: str) -> bool:
        p = os.path.normcase(os.path.normpath(path.strip().strip('"')))
        return p == root or p.startswith(root + os.sep)

    env = {}
    for key, value in environ.items():
        if key.upper().startswith("_PYI_"):
            continue
        if key.upper() == "PATH":
            env[key] = os.pathsep.join(
                p for p in value.split(os.pathsep) if p and not inside(p))
        elif os.path.normcase(bundle) not in os.path.normcase(value):
            env[key] = value
    return env


_dll_dir_lock = threading.Lock()


@contextlib.contextmanager
def _dll_dir_not_inherited():
    """Clear the frozen app's DLL directory while a child is created.

    Only for the instant of CreateProcess: GTK itself still loads DLLs from
    there later (pixbuf loaders, on first use), so it is put back at once.
    """
    if not (FROZEN_BUNDLE and os.name == "nt"):
        yield
        return
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDllDirectoryW.restype = ctypes.c_uint32
    kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    buf = ctypes.create_unicode_buffer(32768)
    with _dll_dir_lock:
        had = kernel32.GetDllDirectoryW(len(buf), buf) > 0
        kernel32.SetDllDirectoryW(None)
        try:
            yield
        finally:
            kernel32.SetDllDirectoryW(buf.value if had else None)


def start_child(argv, **kwargs) -> subprocess.Popen:
    """subprocess.Popen, minus what the frozen .exe set for itself."""
    kwargs["env"] = child_environ(kwargs.get("env") or os.environ, FROZEN_BUNDLE)
    with _dll_dir_not_inherited():
        return subprocess.Popen(argv, **kwargs)


def run_child(argv, *, input=None, timeout=None, capture_output=False,
              **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for a helper whose output we keep (ssh, a hook).

    Started through start_child, and on Windows without a console window:
    the windowed .exe has none to lend, so each would open its own.
    """
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = (kwargs.get("creationflags", 0)
                                   | subprocess.CREATE_NO_WINDOW)
    with start_child(argv, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


# --------------------------------------------------------------------------- #
# pure output parsers, testable everywhere
# --------------------------------------------------------------------------- #
def parse_ps_lines(text: str) -> list[tuple[int, str]]:
    """`ps -eo pid=,args=` output -> [(pid, command line)]."""
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, args = line.partition(" ")
        try:
            rows.append((int(pid), args.strip()))
        except ValueError:
            continue
    return rows


def parse_xrandr_monitors(text: str) -> dict[int, tuple[int, int, int, int]]:
    """`xrandr --listactivemonitors` -> {1-based index: (x, y, w, h)}."""
    out: dict[int, tuple[int, int, int, int]] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+):\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", line)
        if m:
            idx, width, height, x, y = map(int, m.groups())
            out[idx + 1] = (x, y, width, height)
    return out


def parse_arp_windows(text: str, host: str) -> str:
    """Windows `arp -a` output -> the host's MAC as aa:bb:cc:dd:ee:ff, or ''."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == host:
            mac = parts[1].lower().replace("-", ":")
            if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                return mac
    return ""


# xdotool keysyms -> Windows virtual-key codes, for inject.steps' key:<keysym>.
_VK_NAMED = {"Return": 0x0D, "KP_Enter": 0x0D, "Tab": 0x09, "Escape": 0x1B,
             "BackSpace": 0x08, "space": 0x20, "Delete": 0x2E, "Insert": 0x2D,
             "Home": 0x24, "End": 0x23, "Prior": 0x21, "Next": 0x22,
             "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
             **{f"F{n}": 0x6F + n for n in range(1, 13)}}
_VK_MODIFIERS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
                 "super": 0x5B}
# Without the extended flag Windows reads these as their numpad twins.
_VK_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28}


def keysym_chord_to_vks(chord: str) -> list[tuple[int, bool]]:
    """An xdotool chord ("ctrl+a", "Return") -> [(virtual key, extended)].

    Modifiers come first, in the order written. A keysym with no Windows key
    raises ValueError naming it, rather than pressing something else.
    """
    keys: list[tuple[int, bool]] = []
    for part in chord.split("+"):
        name = part.strip()
        vk = _VK_MODIFIERS.get(name.lower()) or _VK_NAMED.get(name)
        if vk is None and len(name) == 1 and name.isascii() and name.isalnum():
            vk = ord(name.upper())
        if vk is None:
            raise ValueError(f"no Windows key for keysym {name!r} in {chord!r}")
        keys.append((vk, vk in _VK_EXTENDED))
    return keys


# --------------------------------------------------------------------------- #
# Linux / X11 — the machine this grew up on
# --------------------------------------------------------------------------- #
class LinuxX11:
    name = "linux-x11"
    window_control = True      # xdotool: search, activate, move, type
    system_shortcuts = True    # Cinnamon custom keybindings via gsettings
    desktop_files = True       # .desktop launcher + hicolor icon theme

    _keyring_backend = None    # lazy (Secret module, schema)

    # ---- processes -------------------------------------------------------- #
    def spawn_detached(self, argv: list[str], env: dict | None = None) -> None:
        e = dict(os.environ)
        if env:
            e.update(env)
        subprocess.Popen(argv, env=e, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def process_command_lines(self) -> list[tuple[int, str]]:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True).stdout
        return parse_ps_lines(out)

    # ---- windows ---------------------------------------------------------- #
    def xdotool(self, *args: str) -> str:
        try:
            return subprocess.run(["xdotool", *args], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    def session_window(self, pid: int, title_pattern: str) -> str:
        # By title alone, as it always was: a flatpak client reports its
        # sandbox's pid in _NET_WM_PID, so the pid cannot narrow it here.
        for w in self.xdotool("search", "--name", title_pattern).splitlines():
            if w.strip():
                return w.strip()
        return ""

    def screenshot_to(self, dest: Path) -> bool:
        if not shutil.which("gnome-screenshot"):
            return False
        subprocess.run(["gnome-screenshot", "--file", str(dest)],
                       capture_output=True)
        try:
            return bool(Path(dest).stat().st_size)
        except OSError:
            return False

    def credential_typing_problem(self) -> str:
        # xdotool cannot inject under Wayland; without this it fails silently.
        session = os.environ.get("XDG_SESSION_TYPE", "x11")
        if session != "x11":
            return (f"session is {session}, not X11 -- credential typing "
                    "will not work (ydotool is the fallback)")
        return ""

    # ---- network / displays ----------------------------------------------- #
    def mac_for_host(self, host: str) -> str:
        neigh = subprocess.run(["ip", "neigh", "show", host],
                               capture_output=True, text=True).stdout
        match = re.search(r"lladdr\s+([0-9a-f:]{17})", neigh)
        return match.group(1) if match else ""

    def monitor_geometry(self) -> dict[int, tuple[int, int, int, int]]:
        listing = subprocess.run(["xrandr", "--listactivemonitors"],
                                 capture_output=True, text=True).stdout
        return parse_xrandr_monitors(listing)

    # ---- desktop conveniences --------------------------------------------- #
    def notify(self, message: str) -> None:
        subprocess.run(["notify-send", "-a", "Remote Connections", "-t", "2500",
                        "Remote Connections", message], capture_output=True)

    def open_path(self, target: str) -> None:
        self.spawn_detached(["xdg-open", str(target)])

    def console_attached(self) -> bool:
        return True     # a terminal-less launch still has stdin for getpass

    # ---- credentials (libsecret; the operator's stored secrets stay put) -- #
    def _backend(self):
        if self._keyring_backend is None:
            try:
                import gi
                gi.require_version("Secret", "1")
                from gi.repository import Secret
                self._keyring_backend = (Secret, Secret.Schema.new(
                    "org.hyper.rcm", Secret.SchemaFlags.NONE,
                    {"app": Secret.SchemaAttributeType.STRING,
                     "key": Secret.SchemaAttributeType.STRING}))
            except Exception:
                self._keyring_backend = (None, None)
        return self._keyring_backend

    def secret_available(self) -> bool:
        return self._backend()[0] is not None

    def secret_get(self, key: str) -> str:
        Secret, schema = self._backend()
        if not Secret:
            return ""
        try:
            return Secret.password_lookup_sync(
                schema, {"app": "rcm", "key": key}, None) or ""
        except Exception:
            return ""

    def secret_set(self, key: str, password: str) -> bool:
        Secret, schema = self._backend()
        if not Secret:
            return False
        return bool(Secret.password_store_sync(
            schema, {"app": "rcm", "key": key}, Secret.COLLECTION_DEFAULT,
            f"rcm credential for {key}", password, None))

    def secret_clear(self, key: str) -> bool:
        Secret, schema = self._backend()
        if not Secret:
            return False
        try:
            return bool(Secret.password_clear_sync(
                schema, {"app": "rcm", "key": key}, None))
        except Exception:
            return False

    # ---- remote Windows PCs (backups) ------------------------------------- #
    _NO_REMOTE = ("backups drive Windows' own SMB and WMI clients, so they run "
                  "from the Windows build of the manager")

    def smb_connect(self, host: str, share: str, user: str,
                    password: str) -> tuple[str, bool]:
        raise PlatformError(self._NO_REMOTE)

    def smb_disconnect(self, unc: str) -> None:
        raise PlatformError(self._NO_REMOTE)

    def remote_start(self, host: str, user: str, password: str,
                     command_line: str, cwd: str) -> int:
        raise PlatformError(self._NO_REMOTE)


# --------------------------------------------------------------------------- #
# Windows — the port's landing strip
# --------------------------------------------------------------------------- #
class _Win32:
    """The user32 calls credential typing needs, the mpr ones backups use to
    reach admin shares, and the process queries session detection uses,
    bound on first use so this module still imports where there is no
    ctypes.windll."""

    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x1
    KEYEVENTF_KEYUP = 0x2
    KEYEVENTF_UNICODE = 0x4

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as wt
        self.ctypes, self.wt = ctypes, wt
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD),
                        ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class MOUSEINPUT(ctypes.Structure):   # never sent; sizes the union
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG),
                        ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _EVENT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wt.DWORD), ("event", _EVENT)]

        self.INPUT, self.KEYBDINPUT = INPUT, KEYBDINPUT
        self.ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u.EnumWindows.argtypes = [self.ENUM_PROC, wt.LPARAM]
        u.GetForegroundWindow.restype = wt.HWND
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.IsIconic.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.BringWindowToTop.argtypes = [wt.HWND]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wt.UINT
        k.GetCurrentThreadId.restype = wt.DWORD
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.K32EnumProcesses.argtypes = [
            ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.POINTER(wt.DWORD)]
        nt = self.ntdll = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [
            wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
            ctypes.POINTER(wt.ULONG)]
        nt.NtQueryInformationProcess.restype = ctypes.c_long

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        self.UNICODE_STRING = UNICODE_STRING

        class NETRESOURCEW(ctypes.Structure):
            _fields_ = [("dwScope", wt.DWORD), ("dwType", wt.DWORD),
                        ("dwDisplayType", wt.DWORD), ("dwUsage", wt.DWORD),
                        ("lpLocalName", wt.LPWSTR), ("lpRemoteName", wt.LPWSTR),
                        ("lpComment", wt.LPWSTR), ("lpProvider", wt.LPWSTR)]

        self.NETRESOURCEW = NETRESOURCEW
        m = self.mpr = ctypes.WinDLL("mpr")
        m.WNetAddConnection2W.argtypes = [ctypes.POINTER(NETRESOURCEW),
                                          wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
        m.WNetAddConnection2W.restype = wt.DWORD
        m.WNetCancelConnection2W.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL]
        m.WNetCancelConnection2W.restype = wt.DWORD

    def add_connection(self, unc: str, user: str, password: str) -> int:
        """Authenticate to a share without mapping a drive letter. 0 = done."""
        resource = self.NETRESOURCEW(dwType=1, lpRemoteName=unc)   # RESOURCETYPE_DISK
        return self.mpr.WNetAddConnection2W(self.ctypes.byref(resource),
                                            password, user, 0)

    def cancel_connection(self, unc: str) -> None:
        self.mpr.WNetCancelConnection2W(unc, 0, False)   # not if files are open

    def process_ids(self) -> list[int]:
        size = 1024
        while True:
            pids = (self.wt.DWORD * size)()
            got = self.wt.DWORD()
            if not self.kernel32.K32EnumProcesses(
                    pids, self.ctypes.sizeof(pids), self.ctypes.byref(got)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            count = got.value // self.ctypes.sizeof(self.wt.DWORD)
            if count < size:       # a full array may have been cut short
                return [p for p in pids[:count] if p]
            size *= 2

    def command_line(self, pid: int) -> str:
        """The process's command line, or "" where Windows will not say.

        ProcessCommandLineInformation (60) needs only query-limited access,
        the same Task Manager's column gets by with.
        """
        ctypes = self.ctypes
        handle = self.kernel32.OpenProcess(0x1000, False, pid)  # query-limited
        if not handle:
            return ""
        try:
            need = self.wt.ULONG(0)
            buf = ctypes.create_string_buffer(512)
            status = self.ntdll.NtQueryInformationProcess(
                handle, 60, buf, len(buf), ctypes.byref(need))
            if status != 0 and need.value > len(buf):   # too small: retry
                buf = ctypes.create_string_buffer(need.value)
                status = self.ntdll.NtQueryInformationProcess(
                    handle, 60, buf, len(buf), ctypes.byref(need))
            if status != 0:
                return ""
            text = self.UNICODE_STRING.from_buffer(buf)
            if not text.Length or not text.Buffer:
                return ""
            return ctypes.wstring_at(text.Buffer, text.Length // 2)
        finally:
            self.kernel32.CloseHandle(handle)

    def top_level_windows(self) -> list[int]:
        found: list[int] = []
        callback = self.ENUM_PROC(lambda hwnd, _: found.append(hwnd or 0) or True)
        self.user32.EnumWindows(callback, 0)
        return [h for h in found if h]

    def visible(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindowVisible(hwnd))

    def title(self, hwnd: int) -> str:
        size = self.user32.GetWindowTextLengthW(hwnd) + 1
        buf = self.ctypes.create_unicode_buffer(size)
        self.user32.GetWindowTextW(hwnd, buf, size)
        return buf.value

    def class_name(self, hwnd: int) -> str:
        buf = self.ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def pid(self, hwnd: int) -> int:
        pid = self.wt.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return pid.value

    def program(self, hwnd: int) -> str:
        """Image name of the process that owns the window, e.g. Radmin.exe."""
        handle = self.kernel32.OpenProcess(0x1000, False, self.pid(hwnd))  # query-limited
        if not handle:
            return ""
        try:
            size = self.wt.DWORD(1024)
            buf = self.ctypes.create_unicode_buffer(size.value)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                        self.ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        r = self.wt.RECT()
        self.user32.GetWindowRect(hwnd, self.ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def foreground(self) -> int:
        return self.user32.GetForegroundWindow() or 0

    def activate(self, hwnd: int, timeout: float = 2.0) -> bool:
        """Bring hwnd to the front; True once Windows reports it there.

        Only the foreground thread may hand focus on, so the switch borrows
        that thread's input queue for its duration (AttachThreadInput).
        """
        u, k = self.user32, self.kernel32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)   # SW_RESTORE
        deadline = time.time() + timeout
        while self.foreground() != hwnd:
            if time.time() > deadline:
                return False
            front = self.foreground()
            theirs = u.GetWindowThreadProcessId(front, None) if front else 0
            mine = k.GetCurrentThreadId()
            attached = bool(theirs and theirs != mine
                            and u.AttachThreadInput(mine, theirs, True))
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    u.AttachThreadInput(mine, theirs, False)
            time.sleep(0.05)
        return True

    def send_keys(self, events: list[tuple[int, int, int]]) -> None:
        """(virtual key, scan or UTF-16 unit, flags) events through SendInput."""
        batch = (self.INPUT * len(events))()
        for slot, (vk, scan, flags) in zip(batch, events):
            slot.type = self.INPUT_KEYBOARD
            slot.event.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        sent = self.user32.SendInput(len(events), batch,
                                     self.ctypes.sizeof(self.INPUT))
        if sent != len(events):
            raise RuntimeError("Windows refused the keystrokes (a prompt running "
                               "as administrator only takes input from an "
                               "elevated program)")


class WindowsNative:
    name = "windows"
    window_control = False
    system_shortcuts = False   # RegisterHotKey belongs to the planned tray agent
    desktop_files = False

    _TRAY_AGENT = ("not available on Windows yet -- window control and "
                   "hotkeys arrive with the tray agent phase of the port plan")

    # ---- processes -------------------------------------------------------- #
    def spawn_detached(self, argv: list[str], env: dict | None = None) -> None:
        e = dict(os.environ)
        if env:
            e.update(env)
        flags = (subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
        start_child(argv, env=e, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=flags)

    def process_command_lines(self) -> list[tuple[int, str]]:
        """Read in this process, never through PowerShell.

        The PowerShell Get-CimInstance query this replaces took 1.5-3 s on a
        quiet PC and 26 s on a busy one, and the manager runs it on its main
        loop every few seconds: the window was "Not responding" most of the
        time. Read from the kernel the same text takes ~35 ms.

        Processes Windows will not reveal (System, other users' unless
        elevated) have no command line. That means "unreadable", not "no
        arguments", so they are dropped: left in, an empty string matches
        web's deliberately unmatchable `^$`.
        """
        w32 = self._win32()
        rows: list[tuple[int, str]] = []
        for pid in w32.process_ids():
            args = w32.command_line(pid).strip()
            if args:
                rows.append((pid, args))
        return rows

    # ---- windows ---------------------------------------------------------- #
    _w32: _Win32 | None = None
    _typing_target = 0     # the window `windowactivate` last brought forward

    def _win32(self) -> _Win32:
        if self._w32 is None:
            self._w32 = _Win32()
        return self._w32

    def xdotool(self, *args: str) -> str:
        """The xdotool subcommands credential typing, calibrate and focusing
        use, done with user32 so rcm keeps one code path for both systems.

        X11's WM_CLASS names the program, while a Windows window class names
        the widget (#32770 is every dialog), so `search --class` also matches
        the owning program's image name: [Rr]admin finds Radmin.exe's prompt
        as it did under Wine. A window that will not come forward fails
        quietly, as with xdotool, but then `type` and `key` refuse: they go
        only to the window `windowactivate` last brought forward, and stop
        once it is no longer in front -- xdotool would type on into whatever
        took focus, and here that can be a password. Moving windows stays
        with the tray agent.
        """
        w32 = self._win32()
        cmd, rest = args[0], list(args[1:])
        if cmd == "search":
            # xdotool compiles its patterns case-insensitively too.
            pattern = re.compile(rest[-1], re.IGNORECASE)
            by_class = "--class" in rest
            hits = []
            for hwnd in w32.top_level_windows():
                if "--onlyvisible" in rest and not w32.visible(hwnd):
                    continue
                names = ((w32.class_name(hwnd), w32.program(hwnd)) if by_class
                         else (w32.title(hwnd),))
                if any(pattern.search(n) for n in names):
                    hits.append(str(hwnd))
            return "\n".join(hits)
        if cmd == "getwindowname":
            return w32.title(int(rest[-1]))
        if cmd == "getwindowfocus":
            return str(w32.foreground())
        if cmd == "getwindowgeometry":
            left, top, right, bottom = w32.rect(int(rest[-1]))
            return (f"WINDOW={rest[-1]}\nX={left}\nY={top}\n"
                    f"WIDTH={right - left}\nHEIGHT={bottom - top}")
        if cmd == "windowactivate":
            hwnd = int(rest[-1])
            self._typing_target = hwnd if w32.activate(hwnd) else 0
            return ""
        if cmd == "type":
            delay = int(rest[rest.index("--delay") + 1]) if "--delay" in rest else 12
            text = rest[rest.index("--") + 1] if "--" in rest else rest[-1]
            units = text.encode("utf-16-le")
            for i in range(0, len(units), 2):
                self._ensure_target_in_front(w32)
                unit = int.from_bytes(units[i:i + 2], "little")
                w32.send_keys([(0, unit, w32.KEYEVENTF_UNICODE),
                               (0, unit, w32.KEYEVENTF_UNICODE | w32.KEYEVENTF_KEYUP)])
                time.sleep(delay / 1000)
            return ""
        if cmd == "key":
            for chord in rest:
                try:
                    keys = keysym_chord_to_vks(chord)
                except ValueError as e:
                    raise RuntimeError(str(e)) from None
                self._ensure_target_in_front(w32)
                down = [(vk, 0, w32.KEYEVENTF_EXTENDEDKEY if ext else 0)
                        for vk, ext in keys]
                up = [(vk, 0, flags | w32.KEYEVENTF_KEYUP)
                      for vk, _, flags in reversed(down)]
                w32.send_keys(down + up)
            return ""
        raise PlatformError(f"window control ({cmd}) is {self._TRAY_AGENT}")

    def _ensure_target_in_front(self, w32: _Win32) -> None:
        if not self._typing_target:
            raise RuntimeError("the prompt would not come to the front -- "
                               "typed nothing")
        if w32.foreground() != self._typing_target:
            raise RuntimeError("the prompt is no longer in front -- stopped "
                               "typing so nothing lands in another window")

    def session_window(self, pid: int, title_pattern: str) -> str:
        """The live session's own window: visible, owned by its process, and
        whose title matches if any does.

        The process decides before the title does. An Explorer window open on
        \\\\host\\c$ names the host in its title too, and a title search alone
        would focus that instead of the RDP session.
        """
        w32 = self._win32()
        mine = [h for h in w32.top_level_windows()
                if w32.pid(h) == pid and w32.visible(h) and w32.title(h)]
        try:
            pattern = re.compile(title_pattern, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(title_pattern), re.IGNORECASE)
        titled = [h for h in mine if pattern.search(w32.title(h))]
        return str((titled or mine or [""])[0])

    def screenshot_to(self, dest: Path) -> bool:
        return False

    def credential_typing_problem(self) -> str:
        return ""

    # ---- network / displays ----------------------------------------------- #
    def mac_for_host(self, host: str) -> str:
        out = subprocess.run(["arp", "-a", host], capture_output=True,
                             text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return parse_arp_windows(out, host)

    def monitor_geometry(self) -> dict[int, tuple[int, int, int, int]]:
        raise PlatformError(f"monitor placement is {self._TRAY_AGENT}")

    # ---- desktop conveniences --------------------------------------------- #
    def notify(self, message: str) -> None:
        print(f"rcm: {message}", file=sys.stderr)

    def open_path(self, target: str) -> None:
        with _dll_dir_not_inherited():
            os.startfile(str(target))    # noqa: S606 -- the Windows "xdg-open"

    def console_attached(self) -> bool:
        """False in a windowed program (pythonw, NetworkComputingHub.exe).

        There no prompt can be answered: getpass would wait forever on a
        console that does not exist, even with stdout piped somewhere. Asked
        of the console's process list, not its window: a console program
        started with CREATE_NO_WINDOW has a console but no window.
        """
        import ctypes
        pids = (ctypes.c_uint32 * 1)()
        return ctypes.windll.kernel32.GetConsoleProcessList(pids, 1) > 0

    # ---- credentials (`keyring` -> Windows Credential Locker) -------------- #
    def _keyring(self):
        try:
            import keyring
            return keyring
        except ImportError:
            return None

    def secret_available(self) -> bool:
        return self._keyring() is not None

    def secret_get(self, key: str) -> str:
        kr = self._keyring()
        if not kr:
            return ""
        try:
            return kr.get_password("rcm", key) or ""
        except Exception:
            return ""

    def secret_set(self, key: str, password: str) -> bool:
        kr = self._keyring()
        if not kr:
            return False
        try:
            kr.set_password("rcm", key, password)
            return True
        except Exception:
            return False

    def secret_clear(self, key: str) -> bool:
        kr = self._keyring()
        if not kr:
            return False
        try:
            kr.delete_password("rcm", key)
            return True
        except Exception:
            return False

    # ---- remote Windows PCs (backups) ------------------------------------- #
    def smb_connect(self, host: str, share: str, user: str,
                    password: str) -> tuple[str, bool]:
        """Authenticate to \\\\host\\share; (UNC path, whether we opened it).

        The password goes to the API, never onto a command line the way
        `net use` would put it. Windows keeps one set of credentials per
        server, so when this PC already holds another (an Explorer window on
        \\\\host\\c$, say) that session is used if it reaches the share, and
        left as it was found.
        """
        unc = f"\\\\{host}\\{share}"
        w32 = self._win32()
        code = w32.add_connection(unc, user, password)
        if code == 0:
            return unc, True
        if code == 1219 and os.path.isdir(unc):
            return unc, False
        raise RuntimeError(f"{unc}: {w32.ctypes.FormatError(code).strip()} "
                           f"(error {code})")

    def smb_disconnect(self, unc: str) -> None:
        self._win32().cancel_connection(unc)

    # Starts one process through WMI over DCOM (RPC on 135 plus the dynamic
    # ports), below-normal priority so production software keeps the CPU.
    # The job, password included, arrives on stdin as JSON.
    _WMI_START = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
    $job = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $secret = ConvertTo-SecureString $job.password -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($job.user, $secret)
    $session = New-CimSession -ComputerName $job.host -Credential $cred `
        -SessionOption (New-CimSessionOption -Protocol Dcom)
    try {
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly `
            -Property @{ PriorityClass = [uint32]16384; ShowWindow = [uint16]0 }
        $r = Invoke-CimMethod -CimSession $session -ClassName Win32_Process `
            -MethodName Create -Arguments @{ CommandLine = $job.command;
                CurrentDirectory = $job.cwd; ProcessStartupInformation = $startup }
        'OK|{0}|{1}' -f $r.ReturnValue, $r.ProcessId
    } finally { Remove-CimSession $session }
} catch { 'ERR|' + $_.Exception.Message }
"""
    _WMI_CREATE_ERRORS = {2: "access denied", 3: "insufficient privilege",
                          8: "unknown failure", 9: "path not found",
                          21: "invalid parameter"}

    def remote_start(self, host: str, user: str, password: str,
                     command_line: str, cwd: str) -> int:
        """Start command_line on host as user; its pid there. Does not wait."""
        import base64
        import json
        encoded = base64.b64encode(self._WMI_START.encode("utf-16-le")).decode()
        job = json.dumps({"host": host, "user": user, "password": password,
                          "command": command_line, "cwd": cwd})
        try:
            reply = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-EncodedCommand", encoded],
                input=job, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW).stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{host}: WMI did not answer within 120 s") from None
        last = next((ln for ln in reversed(reply.splitlines())
                     if ln.startswith(("OK|", "ERR|"))), "ERR|no reply from PowerShell")
        if last.startswith("ERR|"):
            raise RuntimeError(f"{host}: WMI: {last[4:].strip()}")
        _, code, pid = last.split("|")
        if int(code):
            raise RuntimeError(f"{host}: WMI could not start the process: "
                               f"{self._WMI_CREATE_ERRORS.get(int(code), 'code ' + code)}")
        return int(pid)


PLATFORM = WindowsNative() if sys.platform == "win32" else LinuxX11()
