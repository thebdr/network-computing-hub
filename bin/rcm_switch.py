#!/usr/bin/env python3
"""Alt-Tab-style switcher across active sessions. Text only, no thumbnails.

Raised by `rcm next` / `rcm prev` (Super+W / Super+Q). Keep Super held and press
W or Q again to keep moving; release Super and it switches and closes. Tab and
the arrows work too, Enter commits, Escape cancels.

With no modifier held -- run straight from a terminal, or from a script -- there
is no popup at all: it just focuses the next session and exits.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import rcm  # noqa: E402

POLL_MS = 60
DEBUG = bool(os.environ.get("RCM_DEBUG"))
LOG = Path.home() / ".cache" / "rcm-switch.log"


def dbg(msg: str) -> None:
    if not DEBUG:
        return
    try:
        with LOG.open("a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass
# Modifiers worth treating as "held to keep the switcher open".
# MOD4 is the one that matters: on X11 the Super key sets Mod4, and GDK's virtual
# SUPER_MASK does *not* appear in the raw mask from get_device_position. Watching
# only SUPER_MASK means the switcher never sees Super held and never opens.
WATCHED = (Gdk.ModifierType.MOD1_MASK,      # Alt
           Gdk.ModifierType.MOD4_MASK,      # Super, as X actually reports it
           Gdk.ModifierType.SUPER_MASK,
           Gdk.ModifierType.CONTROL_MASK)

CSS = b"""
window.rcm-switch { background-color: rgba(28,28,30,0.97); }
.rcm-switch-title { color: #9aa0a6; font-size: 90%; }
.rcm-row { color: #e8eaed; padding: 6px 10px; }
.rcm-row-sel { background-color: #2b6cb0; color: #ffffff; }
.rcm-proto { color: #9aa0a6; }
.rcm-empty { color: #9aa0a6; padding: 10px; }
"""


class Switcher(Gtk.Window):
    def __init__(self, sessions: list[rcm.Session], start: int = 1):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.get_style_context().add_class("rcm-switch")
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_keep_above(True)
        self.sessions = sessions
        self.index = start % len(sessions) if sessions else 0
        self.rows: list[Gtk.Box] = []
        self.done = False

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame.set_border_width(10)
        self.add(frame)

        title = Gtk.Label(xalign=0, label="Active sessions")
        title.get_style_context().add_class("rcm-switch-title")
        frame.pack_start(title, False, False, 0)

        if not sessions:
            empty = Gtk.Label(label="No active sessions")
            empty.get_style_context().add_class("rcm-empty")
            frame.pack_start(empty, False, False, 0)
        for s in sessions:
            row = Gtk.Box(spacing=10)
            row.get_style_context().add_class("rcm-row")
            proto = Gtk.Label(label=s.proto, xalign=0, width_chars=7)
            proto.get_style_context().add_class("rcm-proto")
            row.pack_start(proto, False, False, 0)
            lab = Gtk.Label(label=s.label, xalign=0, width_chars=16)
            lab.set_ellipsize(Pango.EllipsizeMode.END)
            row.pack_start(lab, False, False, 0)
            host = Gtk.Label(label=s.host, xalign=0)
            host.get_style_context().add_class("rcm-proto")
            row.pack_start(host, False, False, 0)
            frame.pack_start(row, False, False, 0)
            self.rows.append(row)

        self.connect("key-press-event", self.on_key)
        self.connect("key-release-event", self.on_release)
        self.held = Gdk.ModifierType(0)

    # ---- selection ---------------------------------------------------------- #
    def paint(self) -> None:
        for i, row in enumerate(self.rows):
            ctx = row.get_style_context()
            if i == self.index:
                ctx.add_class("rcm-row-sel")
            else:
                ctx.remove_class("rcm-row-sel")

    def step(self, delta: int) -> None:
        if self.rows:
            self.index = (self.index + delta) % len(self.rows)
            self.paint()

    # ---- input -------------------------------------------------------------- #
    def on_key(self, _w, ev) -> bool:
        key = Gdk.keyval_name(ev.keyval) or ""
        dbg(f"key-press {key!r} state={int(ev.state):#07x} index={self.index}")
        if key in ("Escape",):
            self.finish(commit=False)
        elif key in ("Return", "KP_Enter", "space"):
            self.finish(commit=True)
        # w/q are the bound keys: we hold the grab, so Cinnamon never sees the
        # repeat press and we have to advance on them ourselves.
        elif key in ("q", "Q", "Up", "Left", "ISO_Left_Tab"):
            self.step(-1)
        elif key in ("w", "W", "Tab", "Down", "Right", "grave", "quoteleft"):
            self.step(-1 if ev.state & Gdk.ModifierType.SHIFT_MASK else 1)
        return True

    def on_release(self, _w, ev) -> bool:
        # Releasing the modifier that opened us commits, exactly like Alt-Tab.
        name = Gdk.keyval_name(ev.keyval) or ""
        if name.startswith(("Alt", "Super", "Meta", "Control")):
            if self.held:
                self.finish(commit=True)
        return True

    def poll_modifier(self) -> bool:
        """Backstop for the release we may not receive as a key event."""
        if self.done:
            return False
        if not self.held:
            return True
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display else None
        pointer = seat.get_pointer() if seat else None
        root = Gdk.get_default_root_window()
        if not pointer or not root:
            return True
        _win, _x, _y, mask = root.get_device_position(pointer)
        if not (mask & self.held):
            self.finish(commit=True)
            return False
        return True

    def finish(self, commit: bool) -> None:
        if self.done:
            return
        self.done = True
        seat = Gdk.Display.get_default().get_default_seat()
        if seat:
            seat.ungrab()
        target = self.sessions[self.index] if (commit and self.sessions) else None
        self.hide()
        if target:
            rcm.focus_session(target)
        Gtk.main_quit()


def held_modifiers() -> Gdk.ModifierType:
    display = Gdk.Display.get_default()
    seat = display.get_default_seat() if display else None
    root = Gdk.get_default_root_window()
    if not seat or not root:
        return Gdk.ModifierType(0)
    _win, _x, _y, mask = root.get_device_position(seat.get_pointer())
    held = Gdk.ModifierType(0)
    for m in WATCHED:
        if mask & m:
            held |= m
    return held


def run(delta: int = 1) -> int:
    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # Only windowed sessions can be focused; including the rest would let the
    # selection park on an entry it can never move off.
    sessions = [s for s in rcm.sessions() if s.window]
    if not sessions:
        rcm.notify("No active sessions")
        return 0

    held = held_modifiers()
    if not held or len(sessions) == 1:
        # Nothing held means a terminal or a script: no popup, just switch.
        print(rcm.step_session(delta))
        return 0

    w = Switcher(sessions, start=delta)
    w.held = held
    w.show_all()
    w.paint()

    rcm.SWITCH_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    rcm.SWITCH_PIDFILE.write_text(str(os.getpid()))
    try:
        seat = Gdk.Display.get_default().get_default_seat()
        status = seat.grab(w.get_window(), Gdk.SeatCapabilities.KEYBOARD,
                           False, None, None, None, None)
        dbg(f"started delta={delta} held={int(held):#07x} grab={status.value_name} "
            f"sessions={[s.label for s in sessions]}")

        # The repeat press usually reaches Cinnamon, not us: its keybinding is a
        # passive grab that fires regardless of our own. That launches another
        # rcm, which signals us here instead of exiting.
        def bump(step: int) -> bool:
            dbg(f"signal step {step:+d}")
            w.step(step)
            return True  # keep the handler installed

        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, bump, 1)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR2, bump, -1)
        GLib.timeout_add(POLL_MS, w.poll_modifier)
        # Never leave a grabbed, always-on-top window stuck if something goes wrong.
        GLib.timeout_add_seconds(20, lambda: (w.finish(commit=False), False)[1])
        Gtk.main()
    finally:
        try:
            rcm.SWITCH_PIDFILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
