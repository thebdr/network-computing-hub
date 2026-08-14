#!/usr/bin/env python3
"""The OS seam: everything rcm asks of the operating system, in one module.

LinuxX11 is the reference implementation — the exact behaviour the manager
has always had, moved here verbatim. WindowsNative is the port's landing
strip: the pieces that could be written and parse-tested now are real
(detached spawn, process listing, ARP lookup, opening files, credentials
through the `keyring` package), and everything still owned by later phases
of the port plan raises PlatformError naming that plan, so nothing degrades
silently.

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
    """`<pid>|<command line>` lines (the PowerShell process query) -> rows."""
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        pid, sep, args = line.partition("|")
        if not sep:
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
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object "
             "{ '{0}|{1}' -f $_.ProcessId, $_.CommandLine }"],
            capture_output=True, text=True).stdout
        return parse_pipe_processes(out)

    # ---- windows ---------------------------------------------------------- #
    def xdotool(self, *args: str) -> str:
        raise PlatformError(f"window control is {self._TRAY_AGENT}")

    def screenshot_to(self, dest: Path) -> bool:
        return False

    def credential_typing_problem(self) -> str:
        return f"credential typing is {self._TRAY_AGENT}"

    # ---- network / displays ----------------------------------------------- #
    def mac_for_host(self, host: str) -> str:
        out = subprocess.run(["arp", "-a", host], capture_output=True,
                             text=True).stdout
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
