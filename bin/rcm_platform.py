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

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


class PlatformError(RuntimeError):
    """This operation is not available on this platform (yet). Says why."""


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


def parse_pipe_processes(text: str) -> list[tuple[int, str]]:
    """`<pid>|<command line>` lines (the PowerShell process query) -> rows.

    System processes, and other users' unless elevated, report no command line
    at all. That means "unreadable", not "no arguments", so they are dropped:
    left in, an empty string matches web's deliberately unmatchable `^$`.
    """
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        pid, sep, args = line.partition("|")
        if not sep or not args.strip():
            continue
        try:
            rows.append((int(pid.strip()), args.strip()))
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


# --------------------------------------------------------------------------- #
# Windows — the port's landing strip
# --------------------------------------------------------------------------- #
class _Win32:
    """The user32 calls credential typing needs, bound on first use so this
    module still imports where there is no ctypes.windll."""

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
        subprocess.Popen(argv, env=e, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)

    def process_command_lines(self) -> list[tuple[int, str]]:
        # PowerShell writes a pipe in the OEM code page by default, which turns
        # "Café.rdp" into something no saved connection path matches.
        # CREATE_NO_WINDOW: the manager polls this every few seconds, and from
        # a process without a console each run would flash one up.
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ '{0}|{1}' -f $_.ProcessId, $_.CommandLine }"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return parse_pipe_processes(out)

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
        os.startfile(str(target))    # noqa: S606 -- the Windows "xdg-open"

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


PLATFORM = WindowsNative() if sys.platform == "win32" else LinuxX11()
