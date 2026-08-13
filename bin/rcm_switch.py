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
window.rcm-switch { background-color: rgba(28,28,30,0.97); border-radius: 10px; }
.rcm-switch-title { color: #9aa0a6; font-size: 90%; }
.rcm-card { background-color: #2a2a2e; border: 1px solid #46464c;
            border-radius: 6px; padding: 8px 10px; }
.rcm-card-selected { background-color: #2b6cb0; border-color: #d7e3f2; }
.rcm-card-dashed { border-style: dashed; }
.rcm-card-name { color: #ffffff; }
.rcm-dim { color: #9aa0a6; }
.rcm-key { color: #8fd6b4; }
"""


class Switcher(Gtk.Window):
    """Design 5c: every session and every shortcut-bound target, as cards.

    The grid always shows everything — live sessions as solid cards, non-live
    connections that have a keyboard shortcut as dashed ~62%-opacity cards
    labelled "not running — connects". No overflow row: the grid grows.
    Committing a dashed card launches it via rcm.goto(); a solid one focuses.
    """

    COLUMNS = 3

    def __init__(self, entries: list, start: int = 1):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.get_style_context().add_class("rcm-switch")
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_keep_above(True)
        self.entries = entries      # ("live", Session) | ("target", sel, key)
        self.index = start % len(entries) if entries else 0
        self.cards: list[Gtk.Widget] = []
        self.done = False
        self.held = Gdk.ModifierType(0)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        frame.set_border_width(12)
        self.add(frame)

        live_count = sum(1 for e in self.entries if e[0] == "live")
        title_row = Gtk.Box(spacing=20)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"Sessions — {live_count} live · "
                         f"{len(self.entries)} targets")
        title.get_style_context().add_class("rcm-switch-title")
        title_row.pack_start(title, True, True, 0)
        release = Gtk.Label(label="release Super to switch")
        release.get_style_context().add_class("rcm-switch-title")
        title_row.pack_end(release, False, False, 0)
        frame.pack_start(title_row, False, False, 0)

        try:
            registry = rcm.load_protocols()
        except Exception:
            registry = {}
        by_label = {p.label: p for p in registry.values()}

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        frame.pack_start(grid, True, True, 0)
        for position, entry in enumerate(self.entries):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.get_style_context().add_class("rcm-card")
            head = Gtk.Label(xalign=0)
            body = Gtk.Label(xalign=0)
            key_label = Gtk.Label(xalign=0)
            if entry[0] == "live":
                session = entry[1]
                proto = by_label.get(session.proto)
                colour = proto.color if proto and proto.color.startswith("#") \
                    else "#5a6470"
                head.set_markup(
                    f'<span background="{colour}" foreground="#ffffff"> '
                    f'{GLib.markup_escape_text(session.proto)} </span>  '
                    f'<b>{GLib.markup_escape_text(session.label)}</b>')
                head.get_style_context().add_class("rcm-card-name")
                body.set_markup(GLib.markup_escape_text(session.host))
                body.get_style_context().add_class("rcm-dim")
                key = rcm.session_bindings().get(session.label, "")
                key_label.set_text(pretty_binding(key))
            else:
                _kind, sel, key = entry
                connection = rcm.find(sel)
                pid = connection.default_protocol if connection else ""
                proto = registry.get(pid)
                colour = proto.color if proto and proto.color.startswith("#") \
                    else "#5a6470"
                label = proto.label if proto else pid or "?"
                card.get_style_context().add_class("rcm-card-dashed")
                card.set_opacity(0.62)
                head.set_markup(
                    f'<span background="{colour}" foreground="#ffffff"> '
                    f'{GLib.markup_escape_text(label)} </span>  '
                    f'<b>{GLib.markup_escape_text(sel)}</b>')
                body.set_markup("not running — connects")
                body.get_style_context().add_class("rcm-dim")
                key_label.set_text(pretty_binding(key))
            key_label.get_style_context().add_class("rcm-key")
            card.pack_start(head, False, False, 0)
            card.pack_start(body, False, False, 0)
            card.pack_start(key_label, False, False, 0)
            grid.attach(card, position % self.COLUMNS,
                        position // self.COLUMNS, 1, 1)
            self.cards.append(card)

        footer = Gtk.Label(xalign=0)
        footer.set_markup("W next · Q prev · Enter switch · Esc cancel")
        footer.get_style_context().add_class("rcm-switch-title")
        frame.pack_start(footer, False, False, 0)

        self.connect("key-press-event", self.on_key)
        self.connect("key-release-event", self.on_release)

    # ---- selection ---------------------------------------------------------- #
    def paint(self) -> None:
        for position, card in enumerate(self.cards):
            ctx = card.get_style_context()
            if position == self.index:
                ctx.add_class("rcm-card-selected")
                card.set_opacity(1.0)
            else:
                ctx.remove_class("rcm-card-selected")
                if self.entries[position][0] == "target":
                    card.set_opacity(0.62)

    def step(self, delta: int) -> None:
        if self.cards:
            self.index = (self.index + delta) % len(self.cards)
            self.paint()

    # ---- input -------------------------------------------------------------- #
    def on_key(self, _w, ev) -> bool:
        key = Gdk.keyval_name(ev.keyval) or ""
        dbg(f"key-press {key!r} state={int(ev.state):#07x} index={self.index}")
        if key in ("Escape",):
            self.finish(commit=False)
        elif key in ("Return", "KP_Enter", "space"):
            self.finish(commit=True)
        # w/q are the bound keys: while our grab holds, Cinnamon never sees the
        # repeat press and we advance on them ourselves.
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
        chosen = self.entries[self.index] if (commit and self.entries) else None
        self.hide()
        if chosen:
            if chosen[0] == "live":
                rcm.focus_session(chosen[1])
            else:
                rcm.goto(chosen[1])
        Gtk.main_quit()


def pretty_binding(binding: str) -> str:
    """'<Super><Alt>1' -> 'Super+Alt+1' for the card corner."""
    import re as _re
    if not binding:
        return ""
    mods = _re.findall(r"<([^>]+)>", binding)
    key = _re.sub(r"<[^>]+>", "", binding)
    return "+".join(mods + [key])


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

    # Live sessions with windows, then every shortcut-bound connection that is
    # not already among them: the design's "every session and every target".
    sessions = [s for s in rcm.sessions() if s.window]
    live_labels = {s.label for s in sessions}
    entries: list = [("live", s) for s in sessions]
    for sel, binding in rcm.session_bindings().items():
        if sel not in live_labels and rcm.find(sel):
            entries.append(("target", sel, binding))
    if not entries:
        rcm.notify("No sessions and no shortcut-bound targets")
        return 0

    held = held_modifiers()
    if not held or len(entries) == 1:
        # Nothing held means a terminal or a script: no popup, just switch.
        print(rcm.step_session(delta))
        return 0

    w = Switcher(entries, start=delta)
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
