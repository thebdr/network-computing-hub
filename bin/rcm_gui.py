#!/usr/bin/env python3
"""GTK3 manager window for rcm: saved connections on the left, live sessions right."""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import rcm  # noqa: E402
from rcm_help import help_topic as help_topic_gui  # noqa: E402

# VTE gives a real embedded terminal; when the system library is absent the
# terminal features fall back to launching an external terminal instead.
try:
    gi.require_version("Vte", "2.91")
    from gi.repository import Vte  # noqa: E402
    HAVE_VTE = True
except (ValueError, ImportError):
    HAVE_VTE = False

REFRESH_SECONDS = 4
STAGGER = 2  # seconds between launches when several are selected

# background-image:none is needed or the theme's gradient paints over the colour.
CSS_TEMPLATE = """
button.{cls} {{ background-image: none; background-color: {bg};
                color: #ffffff; border-color: {edge}; }}
button.{cls}:hover {{ background-image: none; background-color: {hover}; }}
"""


def _shift(hex_colour: str, factor: float) -> str:
    try:
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return hex_colour
    f = lambda v: max(0, min(255, int(v * factor)))
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


_ACCENT = "#3584e4"   # replaced at startup by the theme's real accent


def resolve_colour(colour: str) -> str:
    """A protocol's colour as a literal hex, since Pango markup needs one."""
    colour = (colour or "").strip()
    return _ACCENT if not colour or colour == "accent" else colour


def protocol_badges(pids, registry, default_pid: str = "") -> str:
    """Pango markup: one coloured chip per enabled protocol.

    The colour lives on the protocol, so the button and the chip are two views
    of the same setting rather than two settings to keep in sync.
    """
    out = []
    for pid in pids:
        pr = registry.get(pid)
        if not pr:
            continue
        bg = resolve_colour(pr.color)
        label = GLib.markup_escape_text(pr.label)
        if pid == default_pid:
            label = f"<u>{label}</u>"
        out.append(f'<span background="{bg}" foreground="#ffffff"> {label} </span>')
    return " ".join(out)


def protocol_css_class(pid: str) -> str:
    return "rcm-proto-" + re.sub(r"[^a-z0-9]+", "-", pid.lower()).strip("-")


def build_protocol_css(protos) -> bytes:
    """One CSS rule per protocol that defines its own colour.

    Colours are config, so the stylesheet has to be generated rather than
    written: a user adding [protocol:anydesk] gets a coloured button with no
    code change. "accent" means fall through to the theme's suggested-action.
    """
    out = []
    for pr in protos.values():
        colour = (pr.color or "").strip()
        if not colour or colour == "accent" or not colour.startswith("#"):
            continue
        out.append(CSS_TEMPLATE.format(cls=protocol_css_class(pr.id), bg=colour,
                                       edge=_shift(colour, 0.8),
                                       hover=_shift(colour, 1.18)))
    return "".join(out).encode()


def pretty_key(binding: str) -> str:
    """'<Super><Alt>1' -> 'Super+Alt+1'. Empty stays empty."""
    if not binding:
        return ""
    mods = re.findall(r"<([^>]+)>", binding)
    key = re.sub(r"<[^>]+>", "", binding)
    return "+".join([m.replace("Primary", "Ctrl") for m in mods] + [key])


def install_css(protos=None, widget=None) -> None:
    # Pull the theme's real accent so "accent" protocols match their button.
    global _ACCENT
    if widget is not None:
        ok, rgba = widget.get_style_context().lookup_color("theme_selected_bg_color")
        if ok:
            _ACCENT = "#{:02x}{:02x}{:02x}".format(
                int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))
    prov = Gtk.CssProvider()
    prov.load_from_data(build_protocol_css(protos or {}))
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

# TreeStore columns. Checkboxes and selection are deliberately independent:
# the Connect buttons act on what is ticked, the context menu on what is
# highlighted, so you can queue up a set and still right-click something else.
(C_CHECK, C_MARK, C_NET, C_LABEL, C_HOST, C_USER, C_PROTOS, C_KEY, C_SEL,
 C_WEIGHT, C_STYLE) = range(11)

def protocols_safe() -> dict:
    """The protocol registry, or {} if the config is broken.

    The window must still open when protocols.conf is wrong -- otherwise the
    only tool for fixing it is unreachable.
    """
    try:
        return rcm.load_protocols()
    except rcm.ConfigError:
        return {}


def event_summary(record: dict) -> str:
    """One readable line for a log event; falls back to raw k=v pairs."""
    kind = record.get("kind", "")
    if kind == "script":
        base = record.get("script", "?")
        if "error" in record:
            return f"{base} — {record['error']}"
        code = record.get("exit_code")
        extra = f" ({record['transport']})" if record.get("transport") else ""
        return f"{base} — {'ok' if code == 0 else f'exit {code}'}{extra}"
    if kind == "send-script":
        return f"{record.get('script', '?')} typed into the terminal"
    if kind == "launch":
        text = f"{record.get('protocol', '?')} via {record.get('launcher', '?')}"
        if record.get("via"):
            text += f" (through {record['via']})"
        return text
    if kind == "terminal":
        return f"terminal opened ({record.get('transport', 'ssh')})"
    if kind == "probe":
        return "came up" if record.get("state") == "up" else "went down"
    if kind == "wake":
        return f"magic packet → {record.get('mac', '?')}"
    if kind == "via":
        return (f"tunnel 127.0.0.1:{record.get('local_port')} → "
                f"port {record.get('port')} (exit {record.get('exit_code')})")
    if kind == "pre-hook":
        return f"exit {record.get('exit_code')}: {record.get('command', '')}"
    if kind == "workspace":
        return f"{record.get('name')} — {record.get('members')} member(s)"
    return " ".join(f"{k}={v}" for k, v in record.items()
                    if k not in ("ts", "kind", "sel"))


def proto_label(pid: str) -> str:
    pr = protocols_safe().get(pid)
    return pr.label if pr else pid


class ShortcutButton(Gtk.Button):
    """Click, then press the combo. Gtk.accelerator_name emits exactly the
    "<Super><Alt>1" syntax Cinnamon's keybinding settings expect."""

    def __init__(self, binding: str = ""):
        super().__init__()
        self.binding = binding
        self.capturing = False
        self.set_size_request(160, -1)
        self.refresh()
        self.connect("clicked", self.start)
        self.connect("key-press-event", self.on_key)

    def refresh(self) -> None:
        if self.capturing:
            self.set_label("press keys…")
        else:
            self.set_label(pretty_key(self.binding) or "unset")

    def start(self, *_a) -> None:
        self.capturing = True
        self.refresh()
        self.grab_focus()

    def on_key(self, _w, ev) -> bool:
        if not self.capturing:
            return False
        name = Gdk.keyval_name(ev.keyval) or ""
        if name == "Escape":
            self.capturing = False
            self.refresh()
            return True
        if name in ("BackSpace", "Delete"):
            self.binding = ""
            self.capturing = False
            self.refresh()
            return True
        # Ignore bare modifiers -- wait for a real key to go with them.
        if name.startswith(("Shift", "Control", "Alt", "Super", "Meta", "Hyper", "ISO_")):
            return True
        mods = ev.state & Gtk.accelerator_get_default_mod_mask()
        if not Gtk.accelerator_valid(ev.keyval, mods):
            return True
        self.binding = Gtk.accelerator_name(ev.keyval, mods)
        self.capturing = False
        self.refresh()
        return True


class Win(Gtk.Window):
    def __init__(self):
        super().__init__(title="Remote Connections")
        self.set_default_size(1080, 600)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        hb = Gtk.HeaderBar(title="Remote Connections", show_close_button=True)
        hb.set_subtitle(str(rcm.RDP_DIR))
        self.set_titlebar(hb)

        b = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        b.set_tooltip_text("New connection")
        b.connect("clicked", lambda *_: self.edit_dialog(None))
        hb.pack_start(b)

        b = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        b.set_tooltip_text("Refresh")
        b.connect("clicked", lambda *_: self.reload())
        hb.pack_end(b)

        # Layout switcher (design 6c): a popover of radio rows, one per layout,
        # each with a one-line description. The choice persists as `layout =`
        # in ui.conf. Layouts not yet built in this branch are insensitive.
        self.ui_state = rcm.load_ui_state()
        self.layout_button = Gtk.MenuButton()
        self.layout_button.set_tooltip_text("Window layout — saved in ui.conf")
        self.layout_label = Gtk.Label(label=f"Layout: {self.ui_state['layout'].title()} ▾")
        self.layout_button.add(self.layout_label)
        self.layout_button.set_popover(self.build_layout_popover())
        hb.pack_end(self.layout_button)


        # Tab strip (design 7a): pinned filter queries shared by every layout.
        # "All" is permanent; Ctrl+T (or ＋) pins the current filter; ✕ closes.
        self.query_group = ""
        self.query_text = ""
        self.tab_strip = Gtk.Box(spacing=2)
        self.tab_strip.set_border_width(4)
        outer.pack_start(self.tab_strip, False, False, 0)
        self.rebuild_tab_strip()

        # The window body is owned by the active layout; switching layouts
        # rebuilds this container and nothing else (headerbar, tab strip and
        # status bar are the shared shell).
        self.layout_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.layout_container, True, True, 0)
        self.current_layout = None

        # Status bar: transient messages left, standing config-health warnings
        # right (fed by rcm.config_health(), same source as the Setup badge).
        status_row = Gtk.Box(spacing=10)
        status_row.set_margin_start(10)
        status_row.set_margin_end(10)
        status_row.set_margin_bottom(6)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        status_row.pack_start(self.status, True, True, 0)
        self.health_label = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.END)
        status_row.pack_end(self.health_label, False, False, 0)
        outer.pack_start(status_row, False, False, 0)

        self.connect("key-press-event", self.on_window_key)

        self.apply_layout(self.ui_state["layout"])
        self.start_prober()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    LAYOUT_DESCRIPTIONS = {
        "browse": "filter, list, sidebar toggle · live sessions as cards",
        "inspector": "read-only audit of credentials and config",
        "classic": "the original window",
    }
    IMPLEMENTED_LAYOUTS = rcm.LAYOUTS

    def build_layout_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        group_button = None
        for layout_id in rcm.LAYOUTS:
            row = Gtk.RadioButton.new_from_widget(group_button)
            group_button = group_button or row
            label = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            title = Gtk.Label(xalign=0, label=layout_id.title())
            desc = Gtk.Label(xalign=0)
            desc.set_markup(f"<small>{self.LAYOUT_DESCRIPTIONS[layout_id]}</small>")
            desc.get_style_context().add_class("dim-label")
            label.pack_start(title, False, False, 0)
            label.pack_start(desc, False, False, 0)
            row.add(label)
            row.set_active(layout_id == self.ui_state["layout"])
            row.set_sensitive(layout_id in self.IMPLEMENTED_LAYOUTS)
            row.connect("toggled", self.on_layout_chosen, layout_id)
            box.pack_start(row, False, False, 2)
        logs_row = Gtk.ModelButton(label="Logs…")
        logs_row.connect("clicked", lambda *_: (popover.popdown(), self.show_logs()))
        setup_row = Gtk.ModelButton(label="Setup…")
        setup_row.connect("clicked", lambda *_: (popover.popdown(), self.on_setup()))
        box.pack_start(Gtk.Separator(), False, False, 4)
        box.pack_start(logs_row, False, False, 0)
        box.pack_start(setup_row, False, False, 0)
        foot = Gtk.Label(xalign=0)
        foot.set_markup("<small>saved as <tt>layout =</tt> in ui.conf</small>")
        foot.get_style_context().add_class("dim-label")
        foot.set_margin_top(6)
        box.pack_start(foot, False, False, 0)
        box.show_all()
        popover.add(box)
        return popover

    def on_layout_chosen(self, radio, layout_id: str) -> None:
        if not radio.get_active() or layout_id == self.ui_state["layout"]:
            return
        self.ui_state["layout"] = layout_id
        rcm.save_ui_state(self.ui_state)
        self.layout_label.set_text(f"Layout: {layout_id.title()} ▾")
        self.apply_layout(layout_id)
        self.say(f"layout switched to {layout_id} — saved in ui.conf")

    def rebuild_tab_strip(self) -> None:
        for child in self.tab_strip.get_children():
            child.destroy()

        def add_tab(label: str, group: str, text: str, closable: bool):
            active = (group, text) == (self.query_group, self.query_text)
            button = Gtk.ToggleButton()
            inner = Gtk.Box(spacing=4)
            inner.add(Gtk.Label(label=label))
            button.add(inner)
            button.set_active(active)
            button.connect("toggled", self.on_tab_toggled, group, text)
            self.tab_strip.pack_start(button, False, False, 0)
            if closable:
                close = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                                      Gtk.IconSize.MENU)
                close.set_relief(Gtk.ReliefStyle.NONE)
                close.set_tooltip_text(f"Close tab {label!r}")
                close.connect("clicked", self.on_tab_closed, label)
                inner.add(close)

        add_tab("All", "", "", closable=False)
        for label, group, text in self.ui_state.get("tabs", []):
            add_tab(label, group, text, closable=True)
        pin = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
        pin.set_relief(Gtk.ReliefStyle.NONE)
        pin.set_tooltip_text("Pin the current filter as a tab (Ctrl+T)")
        pin.connect("clicked", lambda *_: self.pin_current_query())
        self.tab_strip.pack_start(pin, False, False, 0)
        self.tab_strip.show_all()

    def on_tab_toggled(self, button, group: str, text: str) -> None:
        if not button.get_active():
            # Re-activate: the strip behaves like radio tabs, one always active.
            if (group, text) == (self.query_group, self.query_text):
                button.set_active(True)
            return
        self.query_group, self.query_text = group, text
        self.rebuild_tab_strip()
        self.reload()

    def on_tab_closed(self, _button, label: str) -> None:
        self.ui_state["tabs"] = [t for t in self.ui_state["tabs"] if t[0] != label]
        rcm.save_ui_state(self.ui_state)
        if not any((g, x) == (self.query_group, self.query_text)
                   for _l, g, x in self.ui_state["tabs"]):
            self.query_group = self.query_text = ""
        self.rebuild_tab_strip()
        self.reload()

    def pin_current_query(self) -> None:
        """Ctrl+T: the current group/text filter becomes a persistent tab."""
        if not (self.query_group or self.query_text):
            self.say("nothing to pin — the All tab is already permanent")
            return
        label = self.query_text or self.query_group.rsplit("/", 1)[-1]
        if any(t[0] == label for t in self.ui_state["tabs"]):
            self.say(f"tab {label!r} already exists")
            return
        self.ui_state["tabs"].append((label, self.query_group, self.query_text))
        rcm.save_ui_state(self.ui_state)
        self.rebuild_tab_strip()
        self.say(f"pinned {label!r} — tabs live in ui.conf")

    def on_window_key(self, _widget, event) -> bool:
        key_name = Gdk.keyval_name(event.keyval)
        if key_name == "F1":
            focused = self.get_focus()
            self.show_help(getattr(focused, "rcm_help_topic", "") if focused
                           else "")
            return True
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and \
                key_name in ("t", "T"):
            self.pin_current_query()
            return True
        return False

    def connection_matches_query(self, c) -> bool:
        query = self.query_group
        if query.startswith("tag:"):
            if query[len("tag:"):] not in c.tags:
                return False
        elif query.startswith("workspace:"):
            if c.sel not in getattr(self, "workspace_filter_members", ()):
                return False
        elif query and not (c.group == query or
                            c.group.startswith(query + "/")):
            return False
        if self.query_text:
            haystack = " ".join([c.sel, c.host, c.username,
                                 " ".join("#" + t for t in c.tags)]).lower()
            if self.query_text.lower() not in haystack:
                return False
        return True

    def refresh_health(self) -> None:
        problems = rcm.config_health()
        if not problems:
            self.health_label.set_text("")
            return
        worst = problems[0]
        colour = "#c04040" if worst.severity == "error" else "#b5890a"
        extra = f"  (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        self.health_label.set_markup(
            f'<span foreground="{colour}">⚠ {GLib.markup_escape_text(worst.message)}'
            f'{extra}</span>')
        self.health_label.set_tooltip_text("\n".join(w.message for w in problems))


    # ------------------------------------------------------------------ #
    # layout engine: five window arrangements over the same shell
    # ------------------------------------------------------------------ #
    def reset_body_references(self) -> None:
        """Destroy the body and forget every widget reference into it.

        The 4s live timer keeps firing while Setup or the first-run checklist
        is showing; anything still pointing at a destroyed widget is a crash
        waiting for the next tick.
        """
        for child in self.layout_container.get_children():
            child.destroy()
        for name in ("tree", "store", "slist", "sstore", "button_box", "sidebar",
                     "setup_badge", "filter_entry", "inspector_store",
                     "cockpit_cards", "cards_revealer"):
            setattr(self, name, None)

    def apply_layout(self, layout_id: str) -> None:
        """Tear down the body and rebuild it as the chosen layout."""
        if layout_id not in rcm.LAYOUTS:
            layout_id = "classic"
        self.reset_body_references()
        self.tree = None
        self.store = None
        self.slist = None
        self.sstore = None
        self.button_box = None
        self.sidebar = None
        self.setup_badge = None
        self.filter_entry = None
        self.inspector_store = None
        self.cockpit_cards = None
        self.flat_list = layout_id == "browse"
        self.highlight_matches = layout_id == "browse"
        self.current_layout = layout_id
        if not rcm.conns_cached(refresh=True) and \
                not getattr(self, "first_run_skipped", False):
            self.layout_container.pack_start(self.build_first_run(), True, True, 0)
            self.layout_container.show_all()
            return
        builder = getattr(self, f"build_{layout_id}_layout")
        self.layout_container.pack_start(builder(), True, True, 0)
        self.layout_container.show_all()
        self.reload()

    @help_topic_gui("first-run", "The first-run checklist",
                    ("first run", "setup", "empty", "checklist"),
                    section="The window")
    def build_first_run(self):
        """With zero connections the window opens as a setup checklist.

        Each step reports its real state — detected clients by name,
        shortcuts installed or not, a DEFAULT credential present or missing —
        and its button does the actual work: Detect scans for the configured
        protocols' programs (recording an exe it finds, like `rcm detect
        --write`), the others open the matching dialog. Create a connection
        or import a CSV and the checklist gives way to the list; Skip shows
        the empty list without nagging again this run. Everything here is
        also a plain config file.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(28)
        title = Gtk.Label()
        title.set_markup("<big><b>Set up your hub</b></big>")
        outer.pack_start(title, False, False, 0)
        progress = Gtk.Label()
        progress.get_style_context().add_class("dim-label")
        outer.pack_start(progress, False, False, 0)

        detected = self.detect_installed_clients(scan=False)
        shortcuts_in = any(i.startswith("rcm-") for i in rcm.parse_gsettings_list(
            rcm.run_gsettings("get", rcm.GS_LIST, "custom-list")))
        have_default = bool(rcm.secret_get("DEFAULT"))
        have_conns = bool(rcm.conns_cached())

        steps = (
            (bool(detected),
             "Detect installed clients"
             + (f" — found {', '.join(detected)}" if detected else ""),
             "Detect…", self.on_first_run_detect),
            (shortcuts_in, "Install keyboard shortcuts — Super+R, Super+W/Q",
             "Install…", self.on_first_run_shortcuts),
            (have_default, "Set default credentials — stored in the keyring",
             "Set…", lambda: (self.on_credentials(),
                              self.apply_layout(self.ui_state["layout"]))),
            (have_conns, "Create your first connection",
             "New…", lambda: (self.edit_dialog(None),
                              self.apply_layout(self.ui_state["layout"]))),
            (have_conns, "…or import a CSV — group,name,host,user,port",
             "Import…", lambda: (self.on_import(),
                                 self.apply_layout(self.ui_state["layout"]))),
        )
        done = sum(1 for state, *_ in steps if state)
        progress.set_markup(f"<small>{done} of {len(steps)} done — everything "
                            "here is also a plain config file</small>")
        for state, text, button_label, action in steps:
            row = Gtk.Box(spacing=10)
            mark = Gtk.Label(label="✓" if state else "○")
            row.pack_start(mark, False, False, 0)
            label = Gtk.Label(xalign=0, label=text)
            label.set_line_wrap(True)
            row.pack_start(label, True, True, 0)
            button = Gtk.Button(label=button_label)
            button.connect("clicked", lambda _b, act=action: act())
            row.pack_start(button, False, False, 0)
            outer.pack_start(row, False, False, 0)

        skip = Gtk.Button(label="Skip — show me the empty list")
        skip.set_relief(Gtk.ReliefStyle.NONE)

        def on_skip(*_a):
            self.first_run_skipped = True
            self.apply_layout(self.ui_state["layout"])
        skip.connect("clicked", on_skip)
        outer.pack_start(skip, False, False, 8)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.CENTER)
        return outer

    def detect_installed_clients(self, scan: bool) -> list[str]:
        """Client names that resolve; scan=True also walks detect paths."""
        found: list[str] = []
        for proto in protocols_safe().values():
            for tmpl in proto.launchers.values():
                exe = rcm.launcher_exe(proto, tmpl)
                if exe and (shutil.which(exe) or Path(exe).is_file()):
                    name = Path(exe).name
                    if name not in found:
                        found.append(name)
            if scan and not proto.exe:
                candidates = rcm.protocol_detect(proto)
                if candidates and Path(candidates[0]).name not in found:
                    found.append(Path(candidates[0]).name)
        return found

    def on_first_run_detect(self) -> None:
        """Scan for clients; record a found exe like `rcm detect --write`."""
        self.say("detecting installed clients…")

        def worker():
            wrote = []
            protos = rcm.load_protocols()
            for proto in protos.values():
                if proto.exe:
                    continue
                candidates = rcm.protocol_detect(proto)
                if candidates:
                    proto.exe = str(candidates[0])
                    parts = candidates[0].parts
                    if "drive_c" in parts:
                        proto.env["WINEPREFIX"] = str(
                            Path(*parts[:parts.index("drive_c")]))
                    wrote.append(f"{proto.id}: {candidates[0]}")
            if wrote:
                rcm.save_protocols(protos)
            names = self.detect_installed_clients(scan=False)
            message = (f"found {', '.join(names)}" if names
                       else "no configured clients found")
            if wrote:
                message += " · recorded " + "; ".join(wrote)

            def apply():
                self.say(message)
                self.apply_layout(self.ui_state["layout"])
            GLib.idle_add(apply)
        threading.Thread(target=worker, daemon=True).start()

    def install_shortcuts(self) -> tuple[int, list[str]]:
        """shortcuts_install with the platform's answer surfaced, not raised."""
        try:
            return rcm.shortcuts_install()
        except rcm.PlatformError as sorry:
            self.say(str(sorry))
            return 0, [str(sorry)]

    def on_first_run_shortcuts(self) -> None:
        count, warnings = self.install_shortcuts()
        self.say(f"{count} shortcut(s) installed"
                 + (f" — {warnings[0]}" if warnings else ""))
        self.apply_layout(self.ui_state["layout"])

    def build_classic_layout(self):
        """The pre-redesign window, kept selectable for continuity."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(660)

        # ---- left: saved connections ---------------------------------------- #
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_border_width(8)
        paned.pack1(left, True, False)

        head = Gtk.Box(spacing=6)
        lbl = Gtk.Label(xalign=0)
        lbl.set_markup("<b>Saved sessions</b>")
        head.pack_start(lbl, False, False, 0)
        self.count_lbl = Gtk.Label(xalign=0)
        self.count_lbl.get_style_context().add_class("dim-label")
        head.pack_start(self.count_lbl, False, False, 0)
        left.pack_start(head, False, False, 0)

        self.store = Gtk.TreeStore(bool, str, str, str, str, str, str, str, str,
                                   int, int)
        self.tree = Gtk.TreeView(model=self.store)
        # Ctrl and Shift range-select come free with MULTIPLE.
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.set_rubber_banding(True)

        toggle = Gtk.CellRendererToggle()
        toggle.set_activatable(True)
        toggle.connect("toggled", self.on_check_toggled)
        col = Gtk.TreeViewColumn("", toggle, active=C_CHECK)
        col.set_min_width(30)
        self.tree.append_column(col)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Live", r, text=C_MARK)
        col.set_min_width(36)
        self.tree.append_column(col)
        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Net", r, markup=C_NET)
        col.set_min_width(36)
        self.tree.append_column(col)

        for idx, title, minw in ((C_LABEL, "Connection", 130),
                                 (C_HOST, "Host", 118), (C_USER, "User", 78),
                                 (C_PROTOS, "Protocols", 168),
                                 (C_KEY, "Key", 86)):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            if idx in (C_PROTOS, C_LABEL, C_HOST):
                col = Gtk.TreeViewColumn(title, r, markup=idx)
            else:
                col = Gtk.TreeViewColumn(title, r, text=idx)
            col.add_attribute(r, "weight", C_WEIGHT)   # bold = a session is live
            col.add_attribute(r, "style", C_STYLE)     # italic = a group header
            col.set_resizable(True)
            col.set_min_width(minw)
            col.set_expand(idx == C_LABEL)
            self.tree.append_column(col)
            if idx == C_PROTOS:
                self.protocols_column = col

        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_tree_click)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        left.pack_start(sw, True, True, 0)

        # Edit / Duplicate / Delete live in the right-click menu, not here.
        # One button per configured protocol; a FlowBox so ten of them wrap
        # instead of squeezing into an unreadable row.
        self.button_box = Gtk.FlowBox()
        self.button_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.button_box.set_max_children_per_line(4)
        self.button_box.set_row_spacing(6)
        self.button_box.set_column_spacing(6)
        self.button_box.set_homogeneous(True)
        left.pack_start(self.button_box, False, False, 0)
        self.rebuild_buttons()

        # ---- right: active sessions ----------------------------------------- #
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_border_width(8)
        paned.pack2(right, False, False)

        lbl = Gtk.Label(xalign=0)
        lbl.set_markup("<b>Active sessions</b>")
        right.pack_start(lbl, False, False, 0)

        # proto, label, host, key, pid, window
        self.sstore = Gtk.ListStore(str, str, str, str, int, str)
        self.slist = Gtk.TreeView(model=self.sstore)
        self.slist.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for i, (title, minw) in enumerate((("", 58), ("Session", 125),
                                           ("Host", 125), ("Key", 95))):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=i)
            col.set_resizable(True)
            col.set_min_width(minw)
            self.slist.append_column(col)
        self.slist.connect("row-activated", lambda *_: self.on_focus())
        sw2 = Gtk.ScrolledWindow()
        sw2.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw2.add(self.slist)
        sw2.set_shadow_type(Gtk.ShadowType.IN)
        right.pack_start(sw2, True, True, 0)

        row3 = Gtk.Box(spacing=6, homogeneous=True)
        right.pack_start(row3, False, False, 0)
        b = Gtk.Button(label="Focus")
        b.connect("clicked", lambda *_: self.on_focus())
        row3.pack_start(b, True, True, 0)
        b = Gtk.Button(label="Disconnect")
        b.get_style_context().add_class("destructive-action")
        b.connect("clicked", lambda *_: self.on_disconnect())
        row3.pack_start(b, True, True, 0)

        return paned

    # ---- shared pieces the new layouts compose ------------------------- #
    def build_connection_list(self):
        """The connection list used by Rail/Spotlight/Cockpit: same columns as
        Classic, but flat — group changes render as italic heading rows rather
        than expander nesting, and Spotlight highlights filter matches."""
        self.store = Gtk.TreeStore(bool, str, str, str, str, str, str, str, str,
                                   int, int)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.set_rubber_banding(True)
        toggle = Gtk.CellRendererToggle()
        toggle.set_activatable(True)
        toggle.connect("toggled", self.on_check_toggled)
        col = Gtk.TreeViewColumn("", toggle, active=C_CHECK)
        col.set_min_width(30)
        self.tree.append_column(col)
        live_renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Live", live_renderer, text=C_MARK)
        col.set_min_width(36)
        self.tree.append_column(col)
        net_renderer = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Net", net_renderer, markup=C_NET)
        col.set_min_width(36)
        self.tree.append_column(col)
        for idx, title, minw in ((C_LABEL, "Connection", 150),
                                 (C_HOST, "Host", 118), (C_USER, "User", 78),
                                 (C_PROTOS, "Protocols", 168), (C_KEY, "Key", 86)):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            if idx in (C_LABEL, C_HOST, C_PROTOS):
                col = Gtk.TreeViewColumn(title, renderer, markup=idx)
            else:
                col = Gtk.TreeViewColumn(title, renderer, text=idx)
            col.add_attribute(renderer, "weight", C_WEIGHT)
            col.add_attribute(renderer, "style", C_STYLE)
            col.set_resizable(True)
            col.set_min_width(minw)
            col.set_expand(idx == C_LABEL)
            self.tree.append_column(col)
            if idx == C_PROTOS:
                self.protocols_column = col
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.connect("button-press-event", self.on_tree_click)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        return scroller

    def build_filter_row(self, placeholder: str):
        row = Gtk.Box(spacing=6)
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(placeholder)
        self.filter_entry.set_text(self.query_text)
        self.filter_entry.connect("search-changed", self.on_filter_changed)
        row.pack_start(self.filter_entry, True, True, 0)
        recheck = Gtk.Button(label="Recheck")
        recheck.set_tooltip_text("Re-probe the visible rows now")
        recheck.connect("clicked", lambda *_: self.recheck_visible())
        row.pack_start(recheck, False, False, 0)
        new_button = Gtk.Button(label="New")
        new_button.connect("clicked", lambda *_: self.edit_dialog(None))
        row.pack_start(new_button, False, False, 0)
        return row

    def on_filter_changed(self, entry) -> None:
        self.query_text = entry.get_text().strip()
        self.reload()

    def build_active_pane(self):
        """Active-sessions pane (right side of Classic and Rail)."""
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        pane.set_border_width(8)
        label = Gtk.Label(xalign=0)
        label.set_markup("<b>Active sessions</b>")
        pane.pack_start(label, False, False, 0)
        self.sstore = Gtk.ListStore(str, str, str, str, int, str)
        self.slist = Gtk.TreeView(model=self.sstore)
        self.slist.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for i, (title, minw) in enumerate((("", 58), ("Session", 125),
                                           ("Host", 125), ("Key", 95))):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            col.set_min_width(minw)
            self.slist.append_column(col)
        self.slist.connect("row-activated", lambda *_: self.on_focus())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.slist)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        pane.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(spacing=6, homogeneous=True)
        pane.pack_start(buttons, False, False, 0)
        focus = Gtk.Button(label="Focus")
        focus.connect("clicked", lambda *_: self.on_focus())
        buttons.pack_start(focus, True, True, 0)
        disconnect = Gtk.Button(label="Disconnect")
        disconnect.get_style_context().add_class("destructive-action")
        disconnect.connect("clicked", lambda *_: self.on_disconnect())
        buttons.pack_start(disconnect, True, True, 0)
        return pane

    def build_connect_button_box(self):
        self.button_box = Gtk.FlowBox()
        self.button_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.button_box.set_max_children_per_line(4)
        self.button_box.set_row_spacing(6)
        self.button_box.set_column_spacing(6)
        self.button_box.set_homogeneous(True)
        self.rebuild_buttons()
        return self.button_box

    # ---- Browse --------------------------------------------------------- #
    @help_topic_gui("browse-layout", "The Browse layout",
                    ("browse", "sidebar", "filter", "cards", "live"),
                    section="The window")
    def build_browse_layout(self):
        """Browse is the everyday view; its variants are toggles, not modes.

        The filter is always front and centre, and matches highlight while you
        type. The group sidebar collapses with the ⊞ button (remembered in
        ui.conf), turning the same layout from sidebar-navigation into a pure
        filter view. Live sessions appear as a strip of cards — protocol chip,
        name, Focus/Disconnect — only while any exist, so an idle window gives
        every pixel to the list. The Live and per-protocol chips beside the
        filter narrow the list to matching rows.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)

        # Live sessions: present only when there is something to show.
        self.cards_revealer = Gtk.Revealer()
        self.cockpit_cards = Gtk.FlowBox()
        self.cockpit_cards.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cockpit_cards.set_max_children_per_line(6)
        self.cockpit_cards.set_min_children_per_line(2)
        self.cards_revealer.add(self.cockpit_cards)
        outer.pack_start(self.cards_revealer, False, False, 0)

        row = Gtk.Box(spacing=6)
        sidebar_toggle = Gtk.ToggleButton(label="⊞")
        sidebar_toggle.set_tooltip_text("Show or hide the group sidebar")
        sidebar_toggle.set_active(self.ui_state.get("sidebar", True))
        row.pack_start(sidebar_toggle, False, False, 0)
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(
            "matches name · host · group · user   (Ctrl+T pins as tab)")
        self.filter_entry.set_text(self.query_text)
        self.filter_entry.connect("search-changed", self.on_filter_changed)
        row.pack_start(self.filter_entry, True, True, 0)
        self.live_chip = Gtk.ToggleButton(label="Live")
        self.live_chip.set_tooltip_text("Only rows with a live session")
        self.live_chip.connect("toggled", lambda *_: self.reload())
        row.pack_start(self.live_chip, False, False, 0)
        self.proto_chips = {}
        for proto in protocols_safe().values():
            chip = Gtk.ToggleButton(label=proto.label)
            chip.connect("toggled", lambda *_: self.reload())
            self.proto_chips[proto.id] = chip
            row.pack_start(chip, False, False, 0)
        recheck = Gtk.Button(label="Recheck")
        recheck.set_tooltip_text("Re-probe the visible rows now")
        recheck.connect("clicked", lambda *_: self.recheck_visible())
        row.pack_start(recheck, False, False, 0)
        new_button = Gtk.Button(label="New")
        new_button.connect("clicked", lambda *_: self.edit_dialog(None))
        row.pack_start(new_button, False, False, 0)
        outer.pack_start(row, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_border_width(2)
        self.sidebar = Gtk.TreeView(headers_visible=False)
        self.sidebar.get_style_context().add_class("sidebar")
        self.sidebar_store = Gtk.TreeStore(str, str)
        self.sidebar.set_model(self.sidebar_store)
        self.sidebar.append_column(Gtk.TreeViewColumn(
            "", Gtk.CellRendererText(), markup=0))
        self.sidebar.get_selection().connect("changed", self.on_sidebar_selected)
        self.sidebar.connect("row-activated", self.on_sidebar_activated)
        side_scroller = Gtk.ScrolledWindow()
        side_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroller.add(self.sidebar)
        side.pack_start(side_scroller, True, True, 0)
        side.pack_start(Gtk.Separator(), False, False, 0)
        logs_button = Gtk.Button(label="▤ Logs")
        logs_button.set_relief(Gtk.ReliefStyle.NONE)
        logs_button.connect("clicked", lambda *_: self.show_logs())
        side.pack_start(logs_button, False, False, 0)
        self.setup_badge = Gtk.Button()
        self.setup_badge.set_relief(Gtk.ReliefStyle.NONE)
        self.setup_badge.connect("clicked", lambda *_: self.on_setup())
        side.pack_start(self.setup_badge, False, False, 0)
        paned.pack1(side, False, False)
        paned.pack2(self.build_connection_list(), True, False)
        paned.set_position(190 if sidebar_toggle.get_active() else 0)
        outer.pack_start(paned, True, True, 0)

        def on_sidebar_toggled(button):
            visible = button.get_active()
            paned.set_position(190 if visible else 0)
            side.set_visible(visible)
            if not visible:
                self.query_group = ""
                self.reload()
            self.ui_state["sidebar"] = visible
            rcm.save_ui_state(self.ui_state)
        sidebar_toggle.connect("toggled", on_sidebar_toggled)
        self._browse_side_box = side

        outer.pack_start(self.build_connect_button_box(), False, False, 0)
        return outer

    SIDEBAR_HEADER = "\x00header"

    def on_sidebar_selected(self, selection) -> None:
        model, it = selection.get_selected()
        if not it:
            return
        key = model[it][1]
        if key == self.SIDEBAR_HEADER:
            return
        if key.startswith("workspace:"):
            members = rcm.load_workspaces().get(key[len("workspace:"):], [])
            self.workspace_filter_members = frozenset(m["sel"] for m in members)
        self.query_group = key
        self.reload()

    def on_sidebar_activated(self, _tv, path, _col) -> None:
        """Double-click / Enter on a workspace row connects the whole set."""
        key = self.sidebar_store[path][1]
        if not key.startswith("workspace:"):
            return
        name = key[len("workspace:"):]
        self.say(f"connecting workspace {name}…")

        def worker():
            try:
                lines = rcm.connect_workspace(name)
            except (rcm.ConfigError, RuntimeError) as problem:
                lines = [str(problem)]
            GLib.idle_add(self.say, f"workspace {name}: " + "; ".join(lines))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_sidebar(self) -> None:
        if getattr(self, "sidebar", None) is None:
            return
        self.sidebar_store.clear()
        total = len(rcm.conns_cached())
        all_row = self.sidebar_store.append(
            None, [f"All  <small>({total})</small>", ""])
        nodes = {"": all_row}
        for group in rcm.groups():
            parent = nodes.get(group.rsplit("/", 1)[0] if "/" in group else "")
            count = len(rcm.group_members(group))
            nodes[group] = self.sidebar_store.append(
                parent, [f"{GLib.markup_escape_text(group.rsplit('/', 1)[-1])}"
                         f"  <small>({count})</small>", group])
        tag_counts: dict[str, int] = {}
        for c in rcm.conns_cached():
            for tag in c.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if tag_counts:
            header = self.sidebar_store.append(
                None, ["<small><b>TAGS</b></small>", self.SIDEBAR_HEADER])
            for tag in sorted(tag_counts):
                self.sidebar_store.append(header, [
                    f"#{GLib.markup_escape_text(tag)}"
                    f"  <small>({tag_counts[tag]})</small>", f"tag:{tag}"])
        try:
            workspaces = rcm.load_workspaces()
        except Exception:      # noqa: BLE001 -- sidebar must not die on a typo
            workspaces = {}
        if workspaces:
            header = self.sidebar_store.append(
                None, ["<small><b>WORKSPACES</b></small>", self.SIDEBAR_HEADER])
            for name, members in workspaces.items():
                self.sidebar_store.append(header, [
                    f"▶ {GLib.markup_escape_text(name)}"
                    f"  <small>({len(members)})</small>", f"workspace:{name}"])
        self.sidebar.expand_all()
        problems = len(rcm.config_health())
        self.setup_badge.set_label(
            f"⚙ Setup{f'  ({problems}⚠)' if problems else ''}")
        # Hidden sidebar stays hidden across reloads.
        if not self.ui_state.get("sidebar", True) and \
                getattr(self, "_browse_side_box", None) is not None:
            self._browse_side_box.hide()

    def refresh_cockpit_cards(self) -> None:
        """Paint the live-session card strip; hidden entirely when idle."""
        if getattr(self, "cockpit_cards", None) is None:
            return
        for child in self.cockpit_cards.get_children():
            child.destroy()
        registry = protocols_safe()
        keys = rcm.session_bindings()
        sessions = rcm.sessions()
        if getattr(self, "cards_revealer", None) is not None:
            self.cards_revealer.set_reveal_child(bool(sessions))
        for session in sessions:
            card = Gtk.Frame()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_border_width(8)
            head = Gtk.Label(xalign=0)
            proto = next((q for q in registry.values()
                          if q.label == session.proto), None)
            chip = (f'<span background="{resolve_colour(proto.color)}" '
                    f'foreground="#ffffff"> {GLib.markup_escape_text(session.proto)} '
                    f'</span>  ') if proto else ""
            head.set_markup(chip + f"<b>{GLib.markup_escape_text(session.label)}</b>")
            inner.pack_start(head, False, False, 0)
            sub = Gtk.Label(xalign=0)
            key = pretty_key(keys.get(session.label, ""))
            sub.set_markup(f"<small>{GLib.markup_escape_text(session.host)}"
                           + (f" · {key}" if key else "") + "</small>")
            sub.get_style_context().add_class("dim-label")
            inner.pack_start(sub, False, False, 0)
            actions = Gtk.Box(spacing=8)
            focus = Gtk.Button(label="Focus")
            focus.set_relief(Gtk.ReliefStyle.NONE)
            focus.connect("clicked",
                          lambda _b, sess=session: rcm.focus_session(sess))
            actions.pack_start(focus, False, False, 0)
            end = Gtk.Button(label="Disconnect")
            end.set_relief(Gtk.ReliefStyle.NONE)
            end.get_style_context().add_class("destructive-action")
            end.connect("clicked",
                        lambda _b, sess=session: (rcm.kill_session(sess),
                                                  self.refresh_live()))
            actions.pack_start(end, False, False, 0)
            inner.pack_start(actions, False, False, 0)
            card.add(inner)
            self.cockpit_cards.add(card)
        self.cockpit_cards.show_all()

    def chip_filters_allow(self, c) -> bool:
        """Chip filters beside the Browse filter box (Live / per-protocol)."""
        if getattr(self, "live_chip", None) and self.live_chip.get_active():
            if c.sel not in rcm.active_sels():
                return False
        chips = getattr(self, "proto_chips", None) or {}
        wanted = [pid for pid, chip in chips.items() if chip.get_active()]
        if wanted and not any(pid in c.protocols for pid in wanted):
            return False
        return True

    # ---- Inspector ------------------------------------------------------ #
    def build_inspector_layout(self):
        """Design 8b: read-only audit — where each connection's password comes
        from (its rung on the credential chain), its shortcut, and whether its
        protocols are healthy. Warnings point at Setup."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.inspector_store = Gtk.ListStore(str, str, str, str, str)
        view = Gtk.TreeView(model=self.inspector_store)
        for i, (title, minw, expand) in enumerate(
                (("Connection", 170, True), ("Host", 110, False),
                 ("Password", 210, False), ("Shortcut", 100, False),
                 ("Protocol health", 220, False))):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, renderer, markup=i)
            col.set_resizable(True)
            col.set_min_width(minw)
            col.set_expand(expand)
            view.append_column(col)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(view)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        box.pack_start(scroller, True, True, 0)
        return box

    def reload_inspector(self) -> None:
        self.inspector_store.clear()
        registry = protocols_safe()
        proto_problems = {w.warning_id: w for w in rcm.config_health()}
        keys = rcm.session_bindings()
        warn = '<span foreground="#b5890a">%s</span>'
        error = '<span foreground="#c04040">%s</span>'
        ok = '<span foreground="#26a269">%s</span>'
        for c in rcm.conns_cached():
            if not self.connection_matches_query(c):
                continue
            state, scope = rcm.connection_password_state(c)
            table = rcm.creds_conf_table()
            if state == "own":
                password = ok % "own ✓ (keyring)"
            elif state == "inherited":
                user = (table.get(scope, ("", ""))[0] or c.username or "?")
                password = (f"inherits {GLib.markup_escape_text(scope)} "
                            f"<small>({GLib.markup_escape_text(user)})</small>")
            else:
                password = error % "none — will prompt ⚠"
            health_bits = []
            for pid in c.protocols:
                for wid in (f"exe-missing-{pid}", f"no-detect-{pid}",
                            f"no-launchers-{pid}"):
                    if wid in proto_problems:
                        health_bits.append(warn % GLib.markup_escape_text(
                            proto_problems[wid].message))
            health = " · ".join(health_bits) if health_bits else ok % "ok"
            self.inspector_store.append([
                GLib.markup_escape_text(c.sel),
                GLib.markup_escape_text(c.host),
                password,
                GLib.markup_escape_text(pretty_key(keys.get(c.sel, ""))) or "—",
                health])

    @help_topic_gui("net-probe", "Online probing and Wake-on-LAN",
                    ("net", "online", "offline", "probe", "wake"),
                    section="Connections")
    def start_prober(self) -> None:
        """The Net column shows whether a machine answers at all.

        ● reachable, ○ offline, ◌ not probed yet. The probe is one TCP connect
        to the connection's default-protocol port with a ~1s timeout, and only
        ONE probe socket exists at any moment — visible rows are probed first,
        a full sweep of everything else trickles along behind, and probing
        pauses entirely while the window is hidden. That is what keeps 500
        hosts cheap. "Recheck" re-probes the visible rows on demand; offline
        rows offer ⚡ Wake when a MAC has been learned.
        """
        import threading
        self.net_state: dict = {}
        self.prober_generation = 0

        def worker():
            while True:
                if not self.get_visible():
                    time.sleep(3)
                    continue
                generation = self.prober_generation
                order = [c for c in rcm.conns_cached()
                         if self.connection_matches_query(c)]
                order += [c for c in rcm.conns_cached() if c not in order]
                for c in order:
                    if generation != self.prober_generation:
                        break
                    if not self.get_visible():
                        break
                    reachable = rcm.probe_host(c.host, c.port_for(c.default_protocol))
                    previous = self.net_state.get(c.sel)
                    self.net_state[c.sel] = reachable
                    if previous is not None and previous != reachable:
                        rcm.log_event("probe", sel=c.sel,
                                      state="up" if reachable else "down")
                    GLib.idle_add(self.paint_net_cells)
                    time.sleep(0.4)
                time.sleep(60)   # rest between sweeps

        threading.Thread(target=worker, daemon=True).start()

    def recheck_visible(self) -> None:
        """Toolbar Recheck: forget visible results and let the prober redo them."""
        for c in rcm.conns_cached():
            if self.connection_matches_query(c):
                self.net_state.pop(c.sel, None)
        self.prober_generation += 1
        self.say("re-probing visible rows")

    def net_cell(self, c) -> str:
        state = getattr(self, "net_state", {}).get(c.sel)
        if state is True:
            return '<span foreground="#26a269">●</span>'
        if state is False:
            return '<span foreground="#c04040">○</span>'
        return '<span foreground="#8f9192">◌</span>'

    def paint_net_cells(self) -> bool:
        if self.store is None:
            return False
        def walk(it):
            while it:
                sel = self.store[it][C_SEL]
                if sel:
                    c = rcm.find(sel)
                    if c:
                        self.store[it][C_NET] = self.net_cell(c)
                child = self.store.iter_children(it)
                if child:
                    walk(child)
                it = self.store.iter_next(it)
        walk(self.store.get_iter_first())
        return False

    @help_topic_gui("terminals", "Terminals and scripts",
                    ("terminal", "vte", "ssh", "script", "run"),
                    section="Terminals")
    def open_terminal_tab(self, c, transport: str = "ssh") -> None:
        """Right-click ▸ Open terminal gives any host a shell, embedded.

        With the VTE library present the terminal opens in a tab in the bottom
        pane and shows exact live state — the shell is a child of this process.
        Without VTE it falls back to launching your external terminal. Multiple
        hosts open one tab each. Right-click the terminal for Send script — it
        types a file from scripts/ into the live session, substituting the
        known {placeholders} ({host} {port} {user} {name} {file} {via}) and
        leaving every other brace alone, since shell scripts have plenty of
        their own. {password} is deliberately not substituted: a terminal is
        scrollback, and scrollback is where passwords go to be found.
        """
        if not HAVE_VTE:
            rcm.spawn_detached(["x-terminal-emulator", "-e",
                                f"ssh {c.username}@{c.host}"])
            self.say(f"opened external terminal to {c.sel} (install "
                     "gir1.2-vte-2.91 for embedded tabs)")
            return
        self.ensure_terminal_pane()
        terminal = Vte.Terminal()
        terminal.rcm_conn = c
        terminal.connect("button-press-event", self.on_terminal_click)
        terminal.spawn_async(
            Vte.PtyFlags.DEFAULT, None,
            ["/usr/bin/ssh", f"{c.username}@{c.host}"], None,
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, None, None)
        label = Gtk.Box(spacing=4)
        label.pack_start(Gtk.Label(label=c.sel), False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                              Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        label.pack_start(close, False, False, 0)
        label.show_all()
        page = self.terminal_notebook.append_page(terminal, label)
        close.connect("clicked", lambda *_:
                      self.terminal_notebook.remove_page(
                          self.terminal_notebook.page_num(terminal)))
        terminal.show()
        self.terminal_notebook.set_current_page(page)
        self.terminal_pane.set_reveal_child(True)
        rcm.log_event("terminal", sel=c.sel, transport=transport)

    def on_terminal_click(self, terminal, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        send = Gtk.MenuItem(label="Send script")
        scripts_dir = rcm.APP / "scripts"
        scripts = sorted(f for f in scripts_dir.glob("*") if f.is_file()) \
            if scripts_dir.is_dir() else []
        if scripts:
            sub = Gtk.Menu()
            for script in scripts:
                mi = Gtk.MenuItem(label=script.name)
                mi.connect("activate",
                           lambda _m, path=script:
                           self.send_script_to_terminal(terminal, path))
                sub.append(mi)
            send.set_submenu(sub)
        else:
            send.set_sensitive(False)
        menu.append(send)
        menu.append(Gtk.SeparatorMenuItem())
        copy_item = Gtk.MenuItem(label="Copy")
        copy_item.connect(
            "activate",
            lambda *_: terminal.copy_clipboard_format(Vte.Format.TEXT)
            if hasattr(Vte, "Format") else terminal.copy_clipboard())
        menu.append(copy_item)
        paste_item = Gtk.MenuItem(label="Paste")
        paste_item.connect("activate", lambda *_: terminal.paste_clipboard())
        menu.append(paste_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def send_script_to_terminal(self, terminal, script_path) -> None:
        c = terminal.rcm_conn
        try:
            p = rcm.protocol("ssh")
        except rcm.ConfigError:
            p = rcm.protocol(c.default_protocol)
        vals = rcm.launch_placeholder_values(
            c, p, via=(c.extra.get("rcm-via") or "").strip())
        del vals["password"]     # never into scrollback; {password} stays put
        text = script_path.read_text(errors="replace")
        rendered = re.sub(r"\{(%s)\}" % "|".join(vals),
                          lambda mo: str(vals[mo.group(1)]), text)
        if not rendered.endswith("\n"):
            rendered += "\n"
        self.feed_terminal(terminal, rendered)
        rcm.log_event("send-script", sel=c.sel, script=script_path.name)
        self.say(f"sent {script_path.name} into the {c.sel} terminal")

    @staticmethod
    def feed_terminal(terminal, text: str) -> None:
        data = text.encode()
        try:
            terminal.feed_child(data)
        except TypeError:    # older VTE introspection wants (text, length)
            terminal.feed_child(text, len(data))

    def ensure_terminal_pane(self) -> None:
        if getattr(self, "terminal_pane", None) is not None:
            return
        self.terminal_notebook = Gtk.Notebook()
        self.terminal_notebook.set_scrollable(True)
        self.terminal_pane = Gtk.Revealer()
        self.terminal_pane.add(self.terminal_notebook)
        # The pane lives below the layout body, shared by every layout.
        self.layout_container.get_parent().pack_start(
            self.terminal_pane, False, False, 0)
        self.terminal_pane.show_all()

    @help_topic_gui("script-runner", "Running scripts across hosts",
                    ("script", "run", "ssh", "winrm", "batch"),
                    section="Terminals")
    def run_script_dialog(self, conns: list) -> None:
        """Right-click ▸ Run script sends one script to every selected host.

        Scripts are plain files in scripts/ — hand-editable and git-friendly,
        sent verbatim (no placeholder substitution; that is Send script's
        job). Transport per host: SSH when port 22 answers (key auth — there
        is no terminal to type a password into), else PowerShell over WinRM
        (5985/5986, credentials from the chain; pywinrm installs itself on
        first use). Each host's verdict stays in the dialog — ✓ ok,
        ⟳ running, ✗ with its exit code — and double-clicking a finished row
        opens its captured output under logs/output/. "Stop on first
        failure" skips the rest after a ✗; Abort stops between hosts.
        """
        scripts_dir = rcm.APP / "scripts"
        scripts = sorted(f for f in scripts_dir.glob("*") if f.is_file()) \
            if scripts_dir.is_dir() else []
        if not scripts:
            self._dialog(Gtk.MessageType.INFO, "No scripts",
                         f"Put runnable files in {scripts_dir} first.")
            return
        d = Gtk.Dialog(title=f"Run script on {len(conns)} host(s)",
                       transient_for=self, modal=False)
        close_btn = d.add_button(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        abort_btn = d.add_button("Abort", 1)
        run_btn = d.add_button("Run", Gtk.ResponseType.OK)
        abort_btn.set_sensitive(False)
        d.set_default_response(Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_border_width(10)
        box.set_spacing(6)
        chooser = Gtk.ComboBoxText()
        for script in scripts:
            chooser.append_text(script.name)
        chooser.set_active(0)
        box.add(chooser)
        transport_note = Gtk.Label(xalign=0)
        transport_note.set_markup(
            "<small>transport: SSH if 22 answers, else PowerShell (WinRM) · "
            "sent, run, exit code collected</small>")
        transport_note.get_style_context().add_class("dim-label")
        box.add(transport_note)
        stop_toggle = Gtk.CheckButton(label="Stop on first failure")
        stop_toggle.set_active(True)
        box.add(stop_toggle)
        results = Gtk.ListStore(str, str, str)   # sel, verdict, output path
        view = Gtk.TreeView(model=results)
        for i, title in enumerate(("Host", "Result")):
            view.append_column(Gtk.TreeViewColumn(
                title, Gtk.CellRendererText(), text=i))
        view.set_tooltip_text("double-click a finished row to open its output")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        scroller.add(view)
        box.pack_start(scroller, True, True, 0)
        for c in conns:
            results.append([c.sel, "queued", ""])
        view.connect("row-activated",
                     lambda _tv, path, _col:
                     results[path][2] and rcm.open_path(results[path][2]))

        abort = threading.Event()

        def on_response(_d, response):
            if response == Gtk.ResponseType.OK:
                script_name = chooser.get_active_text()
                if not script_name:
                    return
                run_btn.set_sensitive(False)
                chooser.set_sensitive(False)
                stop_toggle.set_sensitive(False)
                abort_btn.set_sensitive(True)
                self._script_batch(scripts_dir / script_name, conns,
                                   stop_toggle.get_active(), abort, results,
                                   done=lambda: (abort_btn.set_sensitive(False),
                                                 close_btn.grab_focus()))
                return
            if response == 1:            # Abort: between hosts, not mid-host
                abort.set()
                abort_btn.set_sensitive(False)
                return
            abort.set()
            d.destroy()

        d.connect("response", on_response)
        d.show_all()

    def _script_batch(self, script_path, conns: list, stop_on_fail: bool,
                      abort: threading.Event, results, done) -> None:
        """Worker thread: one host after another, verdicts into `results`."""
        output_dir = rcm.LOGS_DIR / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")

        def set_row(i, verdict, out_path=""):
            def apply():
                results[i][1] = verdict
                if out_path:
                    results[i][2] = out_path
            GLib.idle_add(apply)

        def worker():
            counts = {"ok": 0, "fail": 0}
            for i, c in enumerate(conns):
                if abort.is_set():
                    set_row(i, "— aborted")
                    continue
                if counts["fail"] and stop_on_fail:
                    set_row(i, "— skipped")
                    continue
                set_row(i, "⟳ running…")
                out_file = output_dir / f"{rcm.slugify(c.sel)}-{stamp}.log"
                try:
                    code, output, transport = self._script_one_host(
                        c, script_path)
                    out_file.write_text(output)
                    rcm.log_event("script", sel=c.sel, script=script_path.name,
                                  transport=transport, exit_code=code,
                                  output=str(out_file))
                    if code == 0:
                        counts["ok"] += 1
                        set_row(i, f"✓ ok ({transport})", str(out_file))
                    else:
                        counts["fail"] += 1
                        set_row(i, f"✗ exit {code} — double-click for log",
                                str(out_file))
                except Exception as problem:   # noqa: BLE001 -- verdict, not crash
                    counts["fail"] += 1
                    rcm.log_event("script", sel=c.sel, script=script_path.name,
                                  error=str(problem))
                    set_row(i, f"✗ {problem}")
            GLib.idle_add(done)
            GLib.idle_add(self.say,
                          f"{script_path.name}: {counts['ok']} ✓, "
                          f"{counts['fail']} ✗ on {len(conns)} host(s)")

        threading.Thread(target=worker, daemon=True).start()

    def _script_one_host(self, c, script_path) -> tuple[int, str, str]:
        """(exit code, output, transport) for one host: SSH else WinRM."""
        if rcm.probe_host(c.host, 22, timeout=2.0):
            target = f"{c.username}@{c.host}" if c.username else c.host
            with open(script_path) as script:
                completed = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                     target, "bash -s"], stdin=script,
                    capture_output=True, text=True, timeout=300)
            return (completed.returncode,
                    completed.stdout + completed.stderr, "ssh")
        winrm_port = next((p for p in (5985, 5986)
                           if rcm.probe_host(c.host, p, timeout=2.0)), 0)
        if not winrm_port:
            raise RuntimeError("no transport — nothing on 22 (ssh) "
                               "or 5985/5986 (winrm)")
        winrm = self._ensure_pywinrm()
        user, pw = rcm.creds_lookup(c.sel, c.username)
        if not pw or pw == "CHANGEME":
            raise RuntimeError("winrm needs a password on the credential chain")
        scheme = "https" if winrm_port == 5986 else "http"
        session = winrm.Session(
            f"{scheme}://{c.host}:{winrm_port}/wsman", auth=(user, pw),
            transport="ntlm", server_cert_validation="ignore")
        if script_path.suffix.lower() == ".ps1":
            reply = session.run_ps(script_path.read_text(errors="replace"))
        else:
            # Send-then-run, same shape as the sftp path: the bytes land in
            # %TEMP% and cmd runs them, so .bat files behave exactly as if
            # double-clicked on the machine.
            import base64
            b64 = base64.b64encode(script_path.read_bytes()).decode()
            reply = session.run_ps(
                f"$p = Join-Path $env:TEMP 'rcm-{script_path.name}'; "
                f"[IO.File]::WriteAllBytes($p, "
                f"[Convert]::FromBase64String('{b64}')); "
                "& cmd.exe /c $p; exit $LASTEXITCODE")
        output = (reply.std_out + reply.std_err).decode(errors="replace")
        return reply.status_code, output, f"winrm:{winrm_port}"

    def _ensure_pywinrm(self):
        """Import pywinrm, pip-installing --user on first WinRM use."""
        try:
            import winrm
            return winrm
        except ImportError:
            pass
        GLib.idle_add(self.say, "installing pywinrm (first WinRM use)…")
        import sys
        done = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "pywinrm"],
            capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            tail = (done.stderr.strip() or "?").splitlines()[-1]
            raise RuntimeError(f"pip install --user pywinrm failed: {tail}")
        import importlib
        import site
        importlib.invalidate_caches()
        if site.getusersitepackages() not in sys.path:
            sys.path.append(site.getusersitepackages())
        import winrm
        return winrm

    @help_topic_gui("logs-page", "The Logs page", ("logs", "history", "audit"),
                    section="Logs")
    def show_logs(self, sel: str = "") -> None:
        """Every session, script run, probe change and wake is on the Logs page.

        Reached from the Layout menu (Logs…), ▤ Logs under the Browse
        sidebar, or right-click ▸ History — the same page pre-filtered to one
        connection. The chips narrow to sessions / scripts / probes, the text
        box matches any field, and the range reaches back up to 90 days.
        Selecting a row shows every field the event carries, plus the
        captured output for script runs (one file per host per run under
        logs/output/). Export… writes exactly what the filters show, as CSV.
        """
        self.reset_body_references()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(12)
        holder = {"records": [], "filtered": [], "sel": sel}

        bar = Gtk.Box(spacing=8)
        back = Gtk.Button(label="‹ Back")
        back.connect("clicked",
                     lambda *_: self.apply_layout(self.ui_state["layout"]))
        bar.pack_start(back, False, False, 0)
        heading = Gtk.Label()
        heading.set_markup("<b>Logs</b>")
        bar.pack_start(heading, False, False, 0)

        chip_groups = (("All", None), ("Sessions", {"launch", "terminal"}),
                       ("Scripts", {"script", "send-script"}),
                       ("Probes", {"probe", "wake"}), ("Other", "other"))
        named_union = {k for _t, kinds in chip_groups
                       if isinstance(kinds, set) for k in kinds}
        holder["kinds"] = None
        chip_anchor = None
        for title, kinds in chip_groups:
            chip = Gtk.RadioButton.new_with_label_from_widget(chip_anchor, title)
            chip.set_mode(False)     # draw as a toggle, not a dot
            chip_anchor = chip_anchor or chip

            def on_chip(button, kinds=kinds):
                if button.get_active():
                    holder["kinds"] = kinds
                    refilter()
            chip.connect("toggled", on_chip)
            bar.pack_start(chip, False, False, 0)

        entry = Gtk.SearchEntry()
        entry.set_placeholder_text("filter connection, host, text…")
        entry.connect("search-changed", lambda *_: refilter())
        bar.pack_start(entry, True, True, 0)

        days_combo = Gtk.ComboBoxText()
        for label in ("Today", "7 days", "14 days", "30 days", "90 days"):
            days_combo.append_text(label)
        days_combo.set_active(2)

        def on_days(_combo):
            days = {0: 1, 1: 7, 2: 14, 3: 30, 4: 90}[days_combo.get_active()]
            holder["records"] = rcm.read_log_events(days=days)
            refilter()
        days_combo.connect("changed", on_days)
        bar.pack_start(days_combo, False, False, 0)

        export_btn = Gtk.Button(label="Export…")
        bar.pack_start(export_btn, False, False, 0)
        page.pack_start(bar, False, False, 0)

        history_row = Gtk.Box(spacing=6)
        history_chip = Gtk.Button()
        history_chip.set_relief(Gtk.ReliefStyle.NONE)

        def sync_history_chip():
            if holder["sel"]:
                history_chip.set_label(f"history: {holder['sel']}  ✕")
                history_row.show_all()
            else:
                history_row.hide()

        def clear_history(*_a):
            holder["sel"] = ""
            sync_history_chip()
            refilter()
        history_chip.connect("clicked", clear_history)
        history_row.pack_start(history_chip, False, False, 0)
        page.pack_start(history_row, False, False, 0)

        store = Gtk.ListStore(str, str, str, str, int)
        view = Gtk.TreeView(model=store)
        for i, (title, expand) in enumerate((("Time", False), ("Kind", False),
                                             ("Connection", False),
                                             ("Event", True))):
            # Only Event ellipsizes; the narrow columns show whole values.
            renderer = Gtk.CellRendererText(
                ellipsize=Pango.EllipsizeMode.END if expand
                else Pango.EllipsizeMode.NONE)
            col = Gtk.TreeViewColumn(title, renderer, text=i)
            col.set_resizable(True)
            col.set_expand(expand)
            view.append_column(col)
        scroller = Gtk.ScrolledWindow()
        scroller.add(view)
        scroller.set_shadow_type(Gtk.ShadowType.IN)

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        detail_head = Gtk.Box(spacing=8)
        detail_title = Gtk.Label(xalign=0)
        detail_title.set_markup("<small>select an event for its detail</small>")
        detail_title.get_style_context().add_class("dim-label")
        detail_head.pack_start(detail_title, True, True, 0)
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.set_sensitive(False)
        detail_head.pack_end(copy_btn, False, False, 0)
        detail_box.pack_start(detail_head, False, False, 0)
        detail_view = Gtk.TextView(editable=False, monospace=True)
        detail_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        detail_scroller = Gtk.ScrolledWindow()
        detail_scroller.add(detail_view)
        detail_scroller.set_shadow_type(Gtk.ShadowType.IN)
        detail_box.pack_start(detail_scroller, True, True, 0)

        split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        split.pack1(scroller, True, False)
        split.pack2(detail_box, False, True)
        split.set_position(320)
        page.pack_start(split, True, True, 0)

        foot = Gtk.Label(xalign=0)
        foot.set_markup(f"<small>{GLib.markup_escape_text(str(rcm.LOGS_DIR))}"
                        "/YYYY-MM-DD.jsonl · one line per event, plain files, "
                        "rotated daily · script output under logs/output/"
                        "</small>")
        foot.get_style_context().add_class("dim-label")
        page.pack_start(foot, False, False, 0)

        def when(ts: str) -> str:
            from datetime import datetime
            try:
                t = datetime.fromisoformat(ts).astimezone()
            except ValueError:
                return ts
            now = datetime.now().astimezone()
            return (t.strftime("%H:%M:%S") if t.date() == now.date()
                    else t.strftime("%b %d %H:%M"))

        def refilter(*_a):
            text = entry.get_text().strip().lower()
            store.clear()
            holder["filtered"] = []
            for record in holder["records"]:
                if holder["sel"] and record.get("sel") != holder["sel"]:
                    continue
                kind = record.get("kind", "")
                wanted = holder["kinds"]
                if wanted == "other":
                    if kind in named_union:
                        continue
                elif wanted and kind not in wanted:
                    continue
                if text and text not in " ".join(
                        str(v).lower() for v in record.values()):
                    continue
                holder["filtered"].append(record)
                store.append([when(record.get("ts", "")), kind,
                              record.get("sel", ""), event_summary(record),
                              len(holder["filtered"]) - 1])

        def on_row_selected(selection):
            model, it = selection.get_selected()
            if not it:
                return
            record = holder["filtered"][model[it][4]]
            detail_title.set_markup("<b>{}</b>  <small>{}</small>".format(
                GLib.markup_escape_text(
                    record.get("sel", "") or record.get("kind", "")),
                GLib.markup_escape_text(record.get("ts", ""))))
            lines = [f"{k} = {v}" for k, v in record.items()]
            out_path = record.get("output", "")
            if not out_path and record.get("kind") == "script" \
                    and record.get("sel"):
                legacy = (rcm.LOGS_DIR / "output"
                          / f"{rcm.slugify(record['sel'])}.log")
                if legacy.is_file():
                    out_path = str(legacy)
            if out_path and Path(out_path).is_file():
                captured = Path(out_path).read_text(errors="replace")
                lines += ["", f"--- captured output ({out_path}) ---",
                          captured[-20000:]]
            detail_view.get_buffer().set_text("\n".join(lines))
            copy_btn.set_sensitive(True)
        view.get_selection().connect("changed", on_row_selected)

        def do_copy(*_a):
            buffer = detail_view.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(),
                                   buffer.get_end_iter(), False)
            Gtk.Clipboard.get_default(Gdk.Display.get_default()).set_text(
                text, -1)
            self.say("event detail copied")
        copy_btn.connect("clicked", do_copy)

        def do_export(*_a):
            dest = self._file_dialog("Export filtered log",
                                     Gtk.FileChooserAction.SAVE,
                                     name="rcm-log.csv")
            if not dest:
                return
            import csv
            import json
            with open(dest, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["ts", "kind", "connection", "event", "raw"])
                for record in holder["filtered"]:
                    writer.writerow([record.get("ts", ""),
                                     record.get("kind", ""),
                                     record.get("sel", ""),
                                     event_summary(record),
                                     json.dumps(record, ensure_ascii=False)])
            self.say(f"exported {len(holder['filtered'])} event(s) → {dest}")
        export_btn.connect("clicked", do_export)

        holder["records"] = rcm.read_log_events(days=14)
        refilter()
        self.layout_container.pack_start(page, True, True, 0)
        self.layout_container.show_all()
        sync_history_chip()

    def show_connection_history(self, c) -> None:
        """Right-click ▸ History: the Logs page pre-filtered to one host."""
        self.show_logs(sel=c.sel)

    @help_topic_gui("help-window", "The Help window (F1)",
                    ("help", "f1", "guide", "open code"), section="Help")
    def show_help(self, topic_id: str = "") -> None:
        """F1 opens the guide — anywhere, for the control under focus.

        Widgets carry their topic; with none, the index opens. Every topic is
        generated from the source docstring that implements the behaviour and
        ends with an Open-code link straight to that file and line, opened in
        the first goto-capable editor found (persisted once chosen).
        """
        import rcm_help
        d = Gtk.Dialog(title="Help — Remote Connections", transient_for=self)
        d.set_default_size(680, 460)
        box = d.get_content_area()
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        pane.set_position(210)
        box.pack_start(pane, True, True, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_border_width(6)
        search = Gtk.SearchEntry()
        search.set_placeholder_text("search help…")
        side.pack_start(search, False, False, 0)
        topics_store = Gtk.TreeStore(str, str)   # title, topic_id
        topics_view = Gtk.TreeView(model=topics_store, headers_visible=False)
        topics_view.append_column(Gtk.TreeViewColumn(
            "", Gtk.CellRendererText(), text=0))
        side_scroller = Gtk.ScrolledWindow()
        side_scroller.add(topics_view)
        side.pack_start(side_scroller, True, True, 0)
        pane.pack1(side, False, False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_border_width(10)
        title_label = Gtk.Label(xalign=0)
        body_label = Gtk.Label(xalign=0, yalign=0)
        body_label.set_line_wrap(True)
        body_label.set_selectable(True)
        body_scroller = Gtk.ScrolledWindow()
        body_scroller.add(body_label)
        source_button = Gtk.Button(label="")
        source_button.set_relief(Gtk.ReliefStyle.NONE)
        content.pack_start(title_label, False, False, 0)
        content.pack_start(body_scroller, True, True, 0)
        content.pack_start(source_button, False, False, 0)
        pane.pack2(content, True, False)

        current = {"topic": None}

        def show_topic(topic):
            current["topic"] = topic
            title_label.set_markup(f"<big><b>{GLib.markup_escape_text(topic.title)}"
                                   "</b></big>")
            body_label.set_text(topic.body)
            source_button.set_label(f"⌁ Open the code — {topic.source_file}:"
                                    f"{topic.source_line} {topic.symbol}()")

        def fill(query=""):
            topics_store.clear()
            hits = {t.topic_id for t in rcm_help.search_topics(query)}
            for section, topics in rcm_help.topics_by_section().items():
                visible = [t for t in topics if t.topic_id in hits]
                if not visible:
                    continue
                parent = topics_store.append(None, [section, ""])
                for topic in visible:
                    topics_store.append(parent, [topic.title, topic.topic_id])
            topics_view.expand_all()

        def on_select(selection):
            model, it = selection.get_selected()
            if it and model[it][1]:
                show_topic(rcm_help.HELP_TOPICS[model[it][1]])
        topics_view.get_selection().connect("changed", on_select)
        search.connect("search-changed", lambda e: fill(e.get_text()))

        def open_source(*_a):
            topic = current["topic"]
            if not topic:
                return
            status = rcm.open_in_editor(topic.source_file, topic.source_line)
            if status:
                self.say(status)
                return
            advice = Gtk.MessageDialog(
                transient_for=d, modal=True, message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.NONE, text="No editor we recognise")
            advice.format_secondary_text(
                "To jump to a file and line, install a goto-capable editor "
                "(VS Code, Sublime, gedit, Kate). Or open the file in a plain "
                f"text editor — the topic lives at {topic.source_file} "
                f"line {topic.source_line}.")
            advice.add_buttons("Open with text editor", 1, Gtk.STOCK_CLOSE, 0)
            if advice.run() == 1:
                rcm.open_path(rcm.APP / topic.source_file)
            advice.destroy()
        source_button.connect("clicked", open_source)

        fill()
        if topic_id and topic_id in rcm_help.HELP_TOPICS:
            show_topic(rcm_help.HELP_TOPICS[topic_id])
        elif rcm_help.HELP_TOPICS:
            show_topic(next(iter(rcm_help.HELP_TOPICS.values())))
        d.show_all()
        d.run()
        d.destroy()

    def rebuild_buttons(self) -> None:
        """Redraw the Connect row from protocols.conf."""
        for child in self.button_box.get_children():
            child.destroy()
        protos = protocols_safe()
        if not protos:
            lbl = Gtk.Label()
            lbl.set_markup("<small>no protocols configured — see Protocols…</small>")
            self.button_box.add(lbl)
        for pr in protos.values():
            b = Gtk.Button(label=f"Connect {pr.label}")
            css = "suggested-action" if pr.color in ("", "accent") \
                else protocol_css_class(pr.id)
            b.get_style_context().add_class(css)
            b.connect("clicked", lambda _w, pid=pr.id: self.connect_selected(pid))
            self.button_box.add(b)
        self.button_box.show_all()

    # ---- helpers ----------------------------------------------------------- #
    def say(self, msg: str) -> None:
        self.status.set_text(msg)

    def _tick(self) -> bool:
        self.refresh_live()
        return True

    def reload(self) -> None:
        rcm.conns_cached(refresh=True)
        if self.current_layout == "inspector":
            self.reload_inspector()
            self.refresh_health()
            return
        if self.store is None:
            return
        keys = rcm.session_bindings()
        ticked = self.checked_sels()
        registry = protocols_safe()
        self.store.clear()

        def highlight(text: str) -> str:
            """Escape, and in Spotlight wrap filter matches in the search tint."""
            escaped = GLib.markup_escape_text(text)
            needle = self.query_text.strip()
            if not (self.highlight_matches and needle):
                return escaped
            pattern = re.compile(re.escape(GLib.markup_escape_text(needle)),
                                 re.IGNORECASE)
            return pattern.sub(
                lambda m: f'<span background="#f8e45c" foreground="#000000">'
                          f'{m.group(0)}</span>', escaped)

        def connection_row(c, parent):
            name_markup = highlight(c.name)
            state, _scope = rcm.connection_password_state(c)
            if state == "none":
                name_markup += ('\n<small><span foreground="#c04040">'
                                'no password — will prompt</span></small>')
            self.store.append(parent, [
                c.sel in ticked, "", self.net_cell(c), name_markup,
                highlight(c.host), c.username,
                protocol_badges(c.protocols, registry, c.default_protocol),
                pretty_key(keys.get(c.sel, "")), c.sel, 400, Pango.Style.NORMAL])

        visible = 0
        if self.flat_list:
            # Flat list with italic dim heading rows on group changes (Rail,
            # Spotlight, Cockpit). Headings carry no C_SEL, so selection and
            # ticking of them is inert by the existing rules.
            last_group = None
            for c in rcm.conns_cached():
                if not self.connection_matches_query(c):
                    continue
                if not self.chip_filters_allow(c):
                    continue
                if c.group != last_group and c.group:
                    heading = GLib.markup_escape_text(c.group.replace("/", " / "))
                    self.store.append(None, [False, "", "", heading, "", "", "",
                                             "", "", 400, Pango.Style.ITALIC])
                    last_group = c.group
                connection_row(c, None)
                visible += 1
        else:
            nodes: dict[str, Gtk.TreeIter] = {}

            def group_iter(path: str):
                if not path:
                    return None
                if path not in nodes:
                    parent = group_iter(path.rsplit("/", 1)[0] if "/" in path else "")
                    nodes[path] = self.store.append(
                        parent, [False, "", "", GLib.markup_escape_text(
                            path.rsplit("/", 1)[-1]), "", "", "", "", "",
                            700, Pango.Style.ITALIC])
                return nodes[path]

            for c in rcm.conns_cached():
                if not self.connection_matches_query(c):
                    continue
                connection_row(c, group_iter(c.group))
                visible += 1
            for path in sorted(nodes, key=lambda g: g.count("/"), reverse=True):
                self._sync_group_check(nodes[path])
            self.tree.expand_all()

        total = len(rcm.conns_cached())
        if getattr(self, "count_lbl", None):
            self.count_lbl.set_text(f"({visible})" if visible == total
                                    else f"({visible} of {total})")
        if self.filter_entry is not None:
            self.filter_entry.set_tooltip_text(f"{visible} of {total} match")
        self.refresh_sidebar()
        self.refresh_live()
        self.refresh_health()

    def refresh_live(self) -> None:
        """Repaint everything that shows liveness, on the 4s timer."""
        if self.current_layout == "inspector":
            return
        self.refresh_cockpit_cards()
        if self.store is None or self.tree is None:
            return
        live = rcm.active_sels()

        if self.slist is not None:
            smodel, spaths = self.slist.get_selection().get_selected_rows()
            keep = {smodel[smodel.get_iter(p)][4] for p in spaths}
            keys = rcm.session_bindings()
            self.sstore.clear()
            for s in rcm.sessions():
                self.sstore.append([s.proto, s.label, s.host,
                                    pretty_key(keys.get(s.label, "")), s.pid,
                                    s.window])
            if keep:
                sel = self.slist.get_selection()
                for row in self.sstore:
                    if row[4] in keep:
                        sel.select_path(self.sstore.get_path(row.iter))

        def paint(it) -> bool:
            any_live = False
            while it:
                child = self.store.iter_children(it)
                if child:
                    lit = paint(child)
                    self.store[it][C_MARK] = "●" if lit else ""
                    self.store[it][C_WEIGHT] = 700   # group headers stay bold
                    any_live |= lit
                else:
                    sel = self.store[it][C_SEL]
                    protos = live.get(sel, [])
                    self.store[it][C_MARK] = "●" if protos else ""
                    self.store[it][C_WEIGHT] = 700 if protos else 400
                    any_live |= bool(protos)
                it = self.store.iter_next(it)
            return any_live

        paint(self.store.get_iter_first())

    def _children(self, it) -> list:
        out, k = [], self.store.iter_children(it)
        while k:
            out.append(k)
            k = self.store.iter_next(k)
        return out

    def _sync_group_check(self, it) -> None:
        kids = self._children(it)
        self.store[it][C_CHECK] = bool(kids) and all(
            self.store[k][C_CHECK] for k in kids)

    def on_check_toggled(self, _renderer, path) -> None:
        """Tick a row. A group carries its whole subtree with it, at any depth."""
        it = self.store.get_iter(path)
        new = not self.store[it][C_CHECK]

        def cascade(node, value):
            self.store[node][C_CHECK] = value
            for k in self._children(node):
                cascade(k, value)
        cascade(it, new)
        # Walk back up: a parent is ticked only when all of its children are.
        parent = self.store.iter_parent(it)
        while parent is not None:
            self._sync_group_check(parent)
            parent = self.store.iter_parent(parent)
        n = len(self.checked_sels())
        self.say(f"{n} connection(s) ticked" if n else "nothing ticked")
        n = len(self.checked_sels())
        self.say(f"{n} connection(s) ticked" if n else "nothing ticked")

    def checked_sels(self) -> set[str]:
        out: set[str] = set()

        def walk(it):
            while it:
                sel = self.store[it][C_SEL]
                if sel and self.store[it][C_CHECK]:
                    out.add(sel)
                child = self.store.iter_children(it)
                if child:
                    walk(child)
                it = self.store.iter_next(it)
        walk(self.store.get_iter_first())
        return out

    def _checked_conns(self) -> list[rcm.Conn]:
        sels = self.checked_sels()
        return [c for c in rcm.conns_cached() if c.sel in sels]

    def _button_targets(self) -> list[rcm.Conn]:
        """What the Connect buttons act on: ticked rows, else the selection.

        Falling back keeps the buttons useful before anything is ticked, without
        making the tickboxes decorative once they are.
        """
        return self._checked_conns() or self._selected_conns()

    def _selected_conns(self) -> list[rcm.Conn]:
        """Selected connections; a selected group row stands for all its members."""
        model, paths = self.tree.get_selection().get_selected_rows()
        out: list[rcm.Conn] = []
        for p in paths:
            it = model.get_iter(p)
            sel = model[it][C_SEL]
            if sel:
                c = rcm.find(sel)
                if c:
                    out.append(c)
            else:
                # A group row: take every connection beneath it, however deep.
                def descend(node):
                    while node:
                        s = model[node][C_SEL]
                        if s:
                            c = rcm.find(s)
                            if c:
                                out.append(c)
                        child = model.iter_children(node)
                        if child:
                            descend(child)
                        node = model.iter_next(node)
                descend(model.iter_children(it))
        seen, uniq = set(), []
        for c in out:
            if c.sel not in seen:
                seen.add(c.sel)
                uniq.append(c)
        return uniq

    def _one_conn(self) -> rcm.Conn | None:
        cs = self._selected_conns()
        if len(cs) != 1:
            self.say("select exactly one connection" if cs else "select a connection first")
            return None
        return cs[0]

    def _selected_sessions(self) -> list[rcm.Session]:
        model, paths = self.slist.get_selection().get_selected_rows()
        out = []
        for p in paths:
            r = model[model.get_iter(p)]
            out.append(rcm.Session(r[0], r[1], r[2], r[4], r[5]))
        return out

    def _dialog(self, kind, text, secondary=""):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=text)
        if secondary:
            d.format_secondary_text(secondary)
        d.run()
        d.destroy()

    # ---- connecting --------------------------------------------------------- #
    def connect_selected(self, proto: str, launcher: str = "",
                         conns: list | None = None) -> None:
        cs = conns if conns is not None else self._button_targets()
        if not cs:
            self.say("tick or select a connection first")
            return
        skipped = [c.sel for c in cs if proto not in c.protocols]
        cs = [c for c in cs if proto in c.protocols]
        if not cs:
            self.say(f"none of the selected connections offer {proto_label(proto)}")
            return
        what = cs[0].sel if len(cs) == 1 else f"{len(cs)} connections"
        self.say(f"connecting {what} over {proto_label(proto)}"
                 + (f" via {launcher}" if launcher else "") + "…"
                 + (f"  (skipped {', '.join(skipped)})" if skipped else ""))

        def work():
            last = ""
            for i, c in enumerate(cs):
                if i:
                    time.sleep(STAGGER)
                try:
                    last = rcm.launch(c, proto, launcher)
                except RuntimeError as e:
                    last = f"{c.sel}: {e}"
                    GLib.idle_add(self.say, last)
            GLib.idle_add(self.say, last if len(cs) == 1
                          else f"launched {len(cs)} {proto_label(proto)} session(s)")
            GLib.idle_add(self.refresh_live)

        threading.Thread(target=work, daemon=True).start()

    def on_row_activated(self, _tv, path, _col) -> None:
        it = self.store.get_iter(path)
        sel = self.store[it][C_SEL]
        c = rcm.find(sel) if sel else None
        if c and c.default_protocol:
            self.connect_selected(c.default_protocol, "", [c])

    def chip_at_position(self, c, cell_x: int):
        """Which protocol chip a click at cell_x landed on, or None.

        Chips are Pango markup inside one text cell, so there are no per-chip
        widgets to receive the click: the cell is measured with the same Pango
        machinery that rendered it, giving each chip its x-range.
        """
        registry = protocols_safe()
        x = 4   # renderer left padding
        layout = self.tree.create_pango_layout("")
        for pid in c.protocols:
            proto = registry.get(pid)
            if not proto:
                continue
            layout.set_markup(f" {GLib.markup_escape_text(proto.label)} ")
            width = layout.get_pixel_size()[0]
            if x <= cell_x <= x + width:
                return pid
            layout.set_markup(" ")
            x += width + layout.get_pixel_size()[0]   # chip + joining space
        return None

    @help_topic_gui("protocol-chips", "Protocol chips",
                    ("chips", "left-click", "launcher menu"),
                    section="Connections")
    def open_chip_menu(self, c, pid: str, event) -> None:
        """The coloured protocol chips on a row are controls, not just labels.

        Left-clicking a chip connects to that row over that protocol with its
        default launcher. Right-clicking opens the launcher menu — every
        command configured for the protocol, the default marked — plus a jump
        to editing the launchers. The chip for the connection's default
        protocol is underlined.
        """
        registry = protocols_safe()
        proto = registry.get(pid)
        if not proto:
            return
        menu = Gtk.Menu()
        header = Gtk.MenuItem(label=f"CONNECT {proto.label.upper()} VIA")
        header.set_sensitive(False)
        menu.append(header)
        for name in proto.launchers:
            label = f"{name}  (default)" if name == proto.default else name
            item = Gtk.MenuItem(label=label)
            item.connect("activate",
                         lambda _m, n=name: self.connect_selected(pid, n, [c]))
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        edit = Gtk.MenuItem(label="Edit launchers…")
        edit.connect("activate", lambda _m: self.on_protocols())
        menu.append(edit)
        menu.show_all()
        menu.popup_at_pointer(event)

    def on_tree_click(self, _w, event):
        info = self.tree.get_path_at_pos(int(event.x), int(event.y))
        if info and getattr(self, "protocols_column", None) is not None:
            path, column, cell_x, _cell_y = info
            if column is self.protocols_column:
                sel = self.store[self.store.get_iter(path)][C_SEL]
                c = rcm.find(sel) if sel else None
                pid = self.chip_at_position(c, cell_x) if c else None
                if c and pid:
                    if event.button == 1:
                        self.connect_selected(pid, "", [c])
                        return True
                    if event.button == 3:
                        self.open_chip_menu(c, pid, event)
                        return True
        if event.button != 3:
            return False
        if not info:
            return False
        path = info[0]
        sel = self.tree.get_selection()
        # Right-clicking inside an existing multi-selection keeps it.
        if not sel.path_is_selected(path):
            sel.unselect_all()
            sel.select_path(path)
        self.popup(event)
        return True

    def popup(self, event) -> None:
        cs = self._selected_conns()
        menu = Gtk.Menu()
        offered = set().union(*(set(c.protocols) for c in cs)) if cs else set()

        for pr in protocols_safe().values():
            top = Gtk.MenuItem(label=f"Connect {pr.label}")
            top.set_sensitive(bool(cs) and pr.id in offered and bool(pr.launchers))
            if len(pr.launchers) <= 1:
                top.connect("activate",
                            lambda _m, pid=pr.id: self.connect_selected(pid, "", cs))
            else:
                sub = Gtk.Menu()
                for nm in pr.launchers:
                    mi = Gtk.MenuItem(label=f"{nm}  (default)" if nm == pr.default else nm)
                    mi.connect("activate",
                               lambda _m, pid=pr.id, n=nm:
                               self.connect_selected(pid, n, cs))
                    sub.append(mi)
                top.set_submenu(sub)
            menu.append(top)

        menu.append(Gtk.SeparatorMenuItem())

        # Terminals (11a): a transport per ssh-ish protocol; one tab per host,
        # offered on ANY connection -- worst case the server refuses.
        term = Gtk.MenuItem(label=f"Open terminal ({len(cs)})" if cs else "Open terminal")
        term.set_sensitive(bool(cs))
        term_sub = Gtk.Menu()
        for proto in protocols_safe().values():
            if proto.id not in ("ssh",) and "ssh" not in proto.detect_process:
                continue
            item = Gtk.MenuItem(label=proto.label)
            item.connect("activate",
                         lambda _m: [self.open_terminal_tab(c) for c in cs])
            term_sub.append(item)
        term.set_submenu(term_sub)
        menu.append(term)

        offline = [c for c in cs
                   if getattr(self, "net_state", {}).get(c.sel) is False
                   and c.extra.get("rcm-mac")]
        wake_item = Gtk.MenuItem(label=f"⚡ Wake ({len(offline)})")
        wake_item.set_sensitive(bool(offline))
        def do_wake(_m):
            for c in offline:
                rcm.wake(c.extra["rcm-mac"], host=c.host)
            self.say(f"magic packet(s) sent to {len(offline)} host(s)")
        wake_item.connect("activate", do_wake)
        menu.append(wake_item)

        run_item = Gtk.MenuItem(label=f"Run script ▸ ({len(cs)})" if cs
                                else "Run script")
        run_item.set_sensitive(bool(cs))
        run_item.connect("activate", lambda _m, sel=cs: self.run_script_dialog(sel))
        menu.append(run_item)

        if len(cs) == 1:
            history = Gtk.MenuItem(label="History")
            history.connect("activate",
                            lambda _m, c=cs[0]: self.show_connection_history(c))
            menu.append(history)

        menu.append(Gtk.SeparatorMenuItem())

        # Export acts on the selection, and a selected group means all of it.
        exp = Gtk.MenuItem(label=f"Export ({len(cs)})" if cs else "Export")
        exp.set_sensitive(bool(cs))
        esub = Gtk.Menu()
        for label, handler in ((".rdp files…", self.export_rdp),
                               ("Radmin phonebook…", self.export_phonebook_sel),
                               ("CSV (re-importable)…", self.export_csv_sel)):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda _m, h=handler, sel=cs: h(sel))
            esub.append(mi)
        exp.set_submenu(esub)
        menu.append(exp)

        menu.append(Gtk.SeparatorMenuItem())
        for label, cb, need_one in (("Edit", self.on_edit, True),
                                    ("Duplicate", self.on_dup, True),
                                    ("Delete", self.on_delete, False)):
            mi = Gtk.MenuItem(label=label)
            mi.set_sensitive(len(cs) == 1 if need_one else bool(cs))
            mi.connect("activate", cb)
            menu.append(mi)

        menu.show_all()
        menu.popup_at_pointer(event)

    # ---- editing ------------------------------------------------------------ #
    def on_edit(self, *_):
        c = self._one_conn()
        if c:
            self.edit_dialog(c)

    def on_dup(self, *_):
        c = self._one_conn()
        if not c:
            return
        name = self._ask_text("Duplicate connection", "New name:", f"{c.name}_copy")
        if not name:
            return
        try:
            rcm.copy_conn(c, name)
        except FileExistsError:
            self._dialog(Gtk.MessageType.ERROR, "That name already exists")
            return
        self.reload()
        self.say(f"duplicated {c.sel} -> {name}")

    def on_delete(self, *_):
        cs = self._selected_conns()
        if not cs:
            self.say("select a connection first")
            return
        names = ", ".join(c.sel for c in cs[:6]) + ("…" if len(cs) > 6 else "")
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"Delete {len(cs)} connection(s)?")
        d.format_secondary_text(f"{names}\n\nRemoves the .rdp file(s). Cannot be undone.")
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.OK:
            emptied = []
            for c in cs:
                emptied += rcm.delete_conn(c)
            rcm.gen_launcher()
            self.reload()
            msg = f"deleted {len(cs)} connection(s)"
            if emptied:
                msg += f"; removed empty group(s): {', '.join(sorted(set(emptied)))}"
            self.say(msg)

    def _ask_text(self, title, label, initial="") -> str:
        d = Gtk.Dialog(title=title, transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_OK, Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(10)
        box.add(Gtk.Label(label=label, xalign=0))
        e = Gtk.Entry(text=initial, activates_default=True)
        box.add(e)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        resp = d.run()
        val = e.get_text().strip()
        d.destroy()
        return val if resp == Gtk.ResponseType.OK else ""

    @help_topic_gui("edit-connection", "Creating and editing a connection",
                    ("edit", "new", "rename", "regroup", "default protocol",
                     "kept values"), section="Connections")
    def edit_dialog(self, c) -> None:
        """One dialog defines a connection: identity, target, protocols, keys.

        Group and Name are always editable — renaming or regrouping moves the
        .rdp file, and the keyring entries and keyboard shortcut follow
        automatically. Typing a new group name creates the group. Tags are
        free labels orthogonal to groups (a host can carry many): typed here
        comma-separated, they appear as #chips under TAGS in the Browse
        sidebar and match in the filter box.

        Each configured protocol is a row: tick it to offer that protocol on
        this connection, pick one row as the Default (what double-click and
        the shortcut use — underlined in the list). A row's parameters show
        while it is ticked, and values are never lost by unticking: they are
        kept, shown as e.g. "port 5901 kept", and return on re-tick.

        The password override and the shortcut live in their own expanders;
        a blank password always means "leave as is". Hooks & gateway holds
        the pre/post commands (pre must exit 0 or the launch stops) and the
        via-gateway picker for hosts that are only reachable through a
        bastion's SSH tunnel.
        """
        new = c is None
        registry = protocols_safe()
        d = Gtk.Dialog(title="New connection" if new else f"Edit — {c.sel}",
                       transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_border_width(12)
        d.get_content_area().add(body)
        columns = Gtk.Box(spacing=16)
        body.pack_start(columns, True, True, 0)

        # ---- left column: identity + target ------------------------------ #
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        columns.pack_start(left, True, True, 0)

        def caption(text):
            label = Gtk.Label(xalign=0)
            label.set_markup(f"<small><b>{text}</b></small>")
            label.get_style_context().add_class("dim-label")
            left.pack_start(label, False, False, 2)

        def entry_row(label_text, value, width=24):
            left.pack_start(Gtk.Label(label=label_text, xalign=0), False, False, 0)
            entry = Gtk.Entry(text=value, activates_default=True)
            entry.set_width_chars(width)
            left.pack_start(entry, False, False, 0)
            return entry

        caption("IDENTITY")
        left.pack_start(Gtk.Label(label="Group", xalign=0), False, False, 0)
        group_combo = Gtk.ComboBoxText.new_with_entry()
        for group in rcm.groups():
            group_combo.append_text(group)
        group_combo.get_child().set_text(c.group if c else "")
        left.pack_start(group_combo, False, False, 0)
        name_entry = entry_row("Name", c.name if c else "")
        move_hint = Gtk.Label(xalign=0)
        move_hint.set_markup("<small>renaming or regrouping moves the .rdp file —\n"
                             "keyring entry and shortcut follow</small>")
        move_hint.get_style_context().add_class("dim-label")
        left.pack_start(move_hint, False, False, 2)
        old_tags = ", ".join(c.tags) if c else ""
        tags_entry = entry_row("Tags", old_tags)
        tags_entry.set_placeholder_text("comma-separated — #chips in the sidebar")
        caption("TARGET")
        host_entry = entry_row("Host", c.host if c else "")
        user_entry = entry_row("Username", c.username if c else "")

        # ---- right column: one bordered row per protocol ------------------ #
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        columns.pack_start(right, True, True, 0)
        proto_caption = Gtk.Label(xalign=0)
        proto_caption.set_markup("<small><b>PROTOCOLS</b></small>")
        proto_caption.get_style_context().add_class("dim-label")
        right.pack_start(proto_caption, False, False, 2)

        enabled = set(c.protocols if c else registry)
        current_default = (c.default_protocol if c else
                           next(iter(registry), ""))
        rows: dict[str, dict] = {}
        radio_anchor = None

        def refresh_row_states(*_a):
            checked = [pid for pid, row in rows.items()
                       if row["check"].get_active()]
            for pid, row in rows.items():
                active = row["check"].get_active()
                row["radio"].set_visible(active)
                row["revealer"].set_reveal_child(active)
                port_text = row["port"].get_text().strip() if row["port"] else ""
                proto_default = str(registry[pid].port or "")
                kept = (not active and port_text and port_text != proto_default)
                row["kept"].set_visible(kept)
                if kept:
                    row["kept"].set_markup(
                        f"<small>port {GLib.markup_escape_text(port_text)} kept</small>")
            if checked and not any(rows[pid]["radio"].get_active()
                                   for pid in checked):
                rows[checked[0]]["radio"].set_active(True)

        for proto in registry.values():
            frame = Gtk.Frame()
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_border_width(6)
            frame.add(row_box)
            head = Gtk.Box(spacing=8)
            check = Gtk.CheckButton(label=proto.label)
            check.set_active(proto.id in enabled)
            head.pack_start(check, False, False, 0)
            kept_label = Gtk.Label(xalign=0)
            kept_label.get_style_context().add_class("dim-label")
            head.pack_start(kept_label, False, False, 0)
            radio = Gtk.RadioButton.new_with_label_from_widget(radio_anchor,
                                                               "Default")
            radio_anchor = radio_anchor or radio
            radio.set_active(proto.id == current_default)
            head.pack_end(radio, False, False, 0)
            row_box.pack_start(head, False, False, 0)
            revealer = Gtk.Revealer()
            params = Gtk.Box(spacing=6)
            params.set_margin_start(24)
            port_entry = None
            if proto.port:
                params.pack_start(Gtk.Label(label="port"), False, False, 0)
                port_entry = Gtk.Entry(width_chars=7, activates_default=True)
                port_entry.set_text(str(c.port_for(proto.id) if c else proto.port))
                port_entry.connect("changed", refresh_row_states)
                params.pack_start(port_entry, False, False, 0)
            revealer.add(params)
            row_box.pack_start(revealer, False, False, 0)
            right.pack_start(frame, False, False, 0)
            rows[proto.id] = {"check": check, "radio": radio,
                              "revealer": revealer, "port": port_entry,
                              "kept": kept_label}
            check.connect("toggled", refresh_row_states)
        default_hint = Gtk.Label(xalign=0)
        default_hint.set_markup("<small>Default = double-click and the shortcut; "
                                "underlined in the list.\nUnchecking keeps "
                                "values — saved as rcm-* keys, clients ignore "
                                "them.</small>")
        default_hint.get_style_context().add_class("dim-label")
        default_hint.set_line_wrap(True)
        right.pack_start(default_hint, False, False, 2)

        # ---- password + shortcut expanders -------------------------------- #
        password_expander = Gtk.Expander(label="Password", expanded=True)
        pw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pw_box.set_border_width(6)
        password_expander.add(pw_box)
        inherited_user = ""
        has_own = bool(c and rcm.secret_get(c.sel))
        if c:
            inherited_user, _pw = rcm.creds_lookup(c.sel, c.username)
        epw = Gtk.Entry(visibility=False, activates_default=True)
        epw.set_placeholder_text("keep this connection's password" if has_own
                                 else "override for this connection")
        pw_box.pack_start(epw, False, False, 0)
        pw_state, pw_scope = (rcm.connection_password_state(c) if c
                              else ("none", ""))
        pw_info = Gtk.Label(xalign=0)
        if has_own:
            info_text = "This connection has its own password."
        elif pw_state == "inherited":
            info_text = (f"Inherits from {pw_scope.split('@')[0]} "
                         f"(user {inherited_user}).")
        else:
            info_text = "No password anywhere on the chain — will prompt."
        pw_info.set_markup(f"<small>{GLib.markup_escape_text(info_text)} "
                           "Blank = leave as is.</small>")
        pw_info.get_style_context().add_class("dim-label")
        pw_box.pack_start(pw_info, False, False, 0)
        clear_pw = Gtk.CheckButton(
            label="Remove this connection's own password"
                  + ("" if has_own else "  (none set)"))
        clear_pw.set_sensitive(has_own)
        pw_box.pack_start(clear_pw, False, False, 0)
        body.pack_start(password_expander, False, False, 0)

        shortcut_expander = Gtk.Expander(label="Shortcut", expanded=True)
        sc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sc_box.set_border_width(6)
        shortcut_expander.add(sc_box)
        cur_binding = rcm.session_bindings().get(c.sel, "") if c else ""
        sc_row = Gtk.Box(spacing=8)
        sc_btn = ShortcutButton(cur_binding)
        sc_row.pack_start(sc_btn, False, False, 0)
        sc_hint = Gtk.Label(xalign=0)
        sc_hint.set_markup("<small>click, then press the combo · Backspace "
                           "clears\nfocuses it if live, connects it if "
                           "not</small>")
        sc_hint.get_style_context().add_class("dim-label")
        sc_row.pack_start(sc_hint, False, False, 0)
        sc_box.pack_start(sc_row, False, False, 0)
        body.pack_start(shortcut_expander, False, False, 0)

        # ---- hooks + via-gateway (10b/10c) -------------------------------- #
        old_pre = c.extra.get("rcm-pre", "") if c else ""
        old_post = c.extra.get("rcm-post", "") if c else ""
        old_via = c.extra.get("rcm-via", "") if c else ""
        try:
            via_names = sorted(rcm.load_vias())
        except rcm.ConfigError:
            via_names = []
        hooks_expander = Gtk.Expander(
            label="Hooks & gateway",
            expanded=bool(old_pre or old_post or old_via))
        hk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hk_box.set_border_width(6)
        hooks_expander.add(hk_box)

        def hook_row(label_text, value, placeholder):
            row = Gtk.Box(spacing=6)
            lab = Gtk.Label(label=label_text, xalign=0)
            lab.set_width_chars(4)
            row.pack_start(lab, False, False, 0)
            entry = Gtk.Entry(text=value, activates_default=True)
            entry.set_placeholder_text(placeholder)
            row.pack_start(entry, True, True, 0)
            hk_box.pack_start(row, False, False, 0)
            return entry

        pre_entry = hook_row("pre", old_pre,
                             "must exit 0 or the launch stops")
        post_entry = hook_row("post", old_post,
                              "optional — runs after the launch")
        via_row = Gtk.Box(spacing=6)
        via_lab = Gtk.Label(label="via", xalign=0)
        via_lab.set_width_chars(4)
        via_row.pack_start(via_lab, False, False, 0)
        via_combo = Gtk.ComboBoxText()
        via_combo.append_text("(direct)")
        for nm in via_names:
            via_combo.append_text(nm)
        if old_via and old_via not in via_names:
            via_combo.append_text(f"{old_via} (missing!)")
            via_combo.set_active(len(via_names) + 1)
        else:
            via_combo.set_active(via_names.index(old_via) + 1 if old_via else 0)
        via_row.pack_start(via_combo, False, False, 0)
        hk_box.pack_start(via_row, False, False, 0)
        hook_hint = Gtk.Label(xalign=0)
        hook_hint.set_markup(
            "<small>placeholders: {host} {port} {user} {name} {file} {via}\n"
            "via = SSH tunnel through a [via:*] gateway from protocols.conf "
            "(key auth)</small>")
        hook_hint.get_style_context().add_class("dim-label")
        hook_hint.set_line_wrap(True)
        hk_box.pack_start(hook_hint, False, False, 0)
        body.pack_start(hooks_expander, False, False, 0)

        d.show_all()
        refresh_row_states()
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return

        # ---- collect ------------------------------------------------------ #
        g = group_combo.get_child().get_text().strip().strip("/")
        n = name_entry.get_text().strip()
        h = host_entry.get_text().strip()
        u = user_entry.get_text().strip()
        checked = [pid for pid, row in rows.items() if row["check"].get_active()]
        default_pid = next((pid for pid in checked
                            if rows[pid]["radio"].get_active()),
                           checked[0] if checked else "")
        ports: dict[str, int] = {}
        for pid, row in rows.items():
            if row["port"] is None:
                continue
            try:
                ports[pid] = int(row["port"].get_text().strip())
            except ValueError:
                pass
        rdp_port = ports.pop("rdp", 3389)
        protos = "" if len(checked) == len(registry) else ",".join(checked)
        new_pw = epw.get_text()
        drop_pw = clear_pw.get_active()
        new_binding = sc_btn.binding
        new_pre = pre_entry.get_text().strip()
        new_post = post_entry.get_text().strip()
        via_choice = via_combo.get_active_text() or "(direct)"
        new_via = ("" if via_choice == "(direct)"
                   else via_choice.removesuffix(" (missing!)"))
        new_tags = ",".join(t.strip() for t in
                            tags_entry.get_text().split(",") if t.strip())
        d.destroy()

        if not n or not h:
            self._dialog(Gtk.MessageType.ERROR, "Name and Host are required")
            return
        if new:
            try:
                rcm.write_rdp(g, n, h, u, rdp_port, ports=ports,
                              protocols=protos, default_proto=default_pid)
            except FileExistsError:
                self._dialog(Gtk.MessageType.ERROR,
                             "That connection already exists")
                return
            rcm.conns_cached(refresh=True)
            target = rcm.find(f"{g}/{n}" if g else n)
            self.say(f"created {target.sel}")
        else:
            target = c
            if (g, n) != (c.group, c.name):
                try:
                    target = rcm.move_connection(c, g, n)
                except (FileExistsError, ValueError, RuntimeError) as e:
                    self._dialog(Gtk.MessageType.ERROR, "Cannot move", str(e))
                    return
                self.say(f"moved {c.sel} → {target.sel}")
            rcm.set_fields(target, host=h, username=u, port=rdp_port,
                           ports=ports, protocols=protos,
                           default_proto=default_pid)

        old_tags_stored = c.extra.get("rcm-tags", "") if c else ""
        for key, old, new_val in (("rcm-pre", old_pre, new_pre),
                                  ("rcm-post", old_post, new_post),
                                  ("rcm-via", old_via, new_via),
                                  ("rcm-tags", old_tags_stored, new_tags)):
            if new_val != old:
                rcm.set_connection_extra(target, key, new_val)

        sel = target.sel
        if drop_pw:
            rcm.secret_clear(sel)
            self.say(f"saved {sel}; its own password removed (now inherited)")
        elif new_pw:
            if rcm.secret_set(sel, new_pw):
                self.say(f"saved {sel} with its own password")
            else:
                self._dialog(Gtk.MessageType.ERROR,
                             "Could not store the password",
                             "No keyring is available.")
        if new_binding != cur_binding:
            gkeys, per_session = rcm.load_shortcuts()
            if new_binding:
                per_session[sel] = new_binding
            else:
                per_session.pop(sel, None)
            rcm.save_shortcuts(gkeys, per_session)
            self.install_shortcuts()
        rcm.gen_launcher()
        self.reload()

    # ---- sessions ----------------------------------------------------------- #
    def on_focus(self) -> None:
        ss = self._selected_sessions()
        if not ss:
            self.say("select a session first")
            return
        self.say(f"focused {ss[0].label}" if rcm.focus_session(ss[0])
                 else f"no window found for {ss[0].label}")

    def on_disconnect(self) -> None:
        ss = self._selected_sessions()
        if not ss:
            self.say("select a session first")
            return
        names = ", ".join(f"{s.proto} {s.label}" for s in ss[:6]) + ("…" if len(ss) > 6 else "")
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"Disconnect {len(ss)} session(s)?")
        d.format_secondary_text(
            f"{names}\n\nEnds the session on the remote machine. Anything unsaved there "
            "stays as it is, but the remote desktop closes.")
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.OK:
            for s in ss:
                rcm.kill_session(s)
            self.say(f"disconnected {len(ss)} session(s)")
            GLib.timeout_add_seconds(1, lambda: (self.refresh_live(), False)[1])

    # ---- export of a selection ------------------------------------------------ #
    def export_rdp(self, conns: list) -> None:
        if not conns:
            return
        d = Gtk.FileChooserDialog(
            title=f"Export {len(conns)} .rdp file(s) to…", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        d.set_current_folder(str(rcm.EXPORT_DIR))
        resp, path = d.run(), d.get_filename()
        d.destroy()
        if resp != Gtk.ResponseType.OK or not path:
            return
        n = rcm.export_rdp_files(conns, Path(path))
        self.say(f"exported {n} .rdp file(s) to {path}")

    def export_csv_sel(self, conns: list) -> None:
        if not conns:
            return
        p = self._file_dialog(f"Export {len(conns)} connection(s) to CSV",
                              Gtk.FileChooserAction.SAVE, name="connections.csv")
        if p:
            n = rcm.export_csv(p, conns)
            self.say(f"exported {n} connection(s) to {p} — re-importable")

    def export_phonebook_sel(self, conns: list) -> None:
        if not conns:
            return
        p = self._file_dialog(f"Export {len(conns)} connection(s) as a Radmin phonebook",
                              Gtk.FileChooserAction.SAVE, name="radmin.rpb",
                              patterns=(("Radmin phonebook", "*.rpb"),))
        if not p:
            return
        try:
            n = rcm.export_phonebook(p, conns)
        except RuntimeError as e:
            self._dialog(Gtk.MessageType.ERROR, "Phonebook export failed", str(e))
            return
        self._dialog(Gtk.MessageType.INFO, f"Wrote {n} record(s)",
                     "Your real phonebook was not touched.")

    # ---- import / export ----------------------------------------------------- #
    def _file_dialog(self, title, action, name=None, patterns=(("CSV", "*.csv"),)):
        d = Gtk.FileChooserDialog(title=title, transient_for=self, action=action)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_OPEN if action == Gtk.FileChooserAction.OPEN
                      else Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        for label, pat in patterns:
            f = Gtk.FileFilter()
            f.set_name(label)
            f.add_pattern(pat)
            d.add_filter(f)
        if name:
            rcm.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            d.set_current_folder(str(rcm.EXPORT_DIR))
            d.set_current_name(name)
        resp = d.run()
        path = d.get_filename()
        d.destroy()
        return Path(path) if resp == Gtk.ResponseType.OK and path else None

    def on_import(self, *_):
        p = self._file_dialog("Import connections from CSV", Gtk.FileChooserAction.OPEN)
        if not p:
            return
        try:
            created, skipped = rcm.import_csv(p, dry_run=True)
        except SystemExit as e:
            self._dialog(Gtk.MessageType.ERROR, "Import failed", str(e))
            return
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"Create {len(created)} connection(s)?")
        detail = "\n".join(created[:12]) or "(nothing to create)"
        if skipped:
            detail += f"\n\nSkipping {len(skipped)}:\n" + "\n".join(skipped[:8])
        d.format_secondary_text(detail)
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.OK and created:
            rcm.import_csv(p)
            rcm.gen_launcher()
            self.reload()
            self.say(f"imported {len(created)} connection(s) from {p.name}")

    def on_export(self, *_):
        p = self._file_dialog("Export connections to CSV", Gtk.FileChooserAction.SAVE,
                              name="connections.csv")
        if p:
            self.say(f"exported {rcm.export_csv(p)} connection(s) to {p}")

    def on_phonebook(self, *_):
        p = self._file_dialog("Export Radmin phonebook", Gtk.FileChooserAction.SAVE,
                              name="radmin.rpb", patterns=(("Radmin phonebook", "*.rpb"),))
        if not p:
            return
        try:
            n = rcm.export_phonebook(p)
        except RuntimeError as e:
            self._dialog(Gtk.MessageType.ERROR, "Phonebook export failed", str(e))
            return
        self._dialog(Gtk.MessageType.INFO, f"Wrote {n} record(s)",
                     "Your real phonebook was not touched. Test the new file with:\n\n"
                     f'WINEPREFIX="{rcm.RADMIN_PREFIX}" {rcm.WINE} '
                     f'"{rcm.RADMIN_EXE}" /pbpath:"{p}"')

    def on_genlauncher(self, *_):
        self.say(f"jump list rebuilt for {rcm.gen_launcher()} connection(s)")

    # ---- protocols ----------------------------------------------------------- #
    def on_protocols(self, *_):
        """Edit protocols.conf: buttons, commands, detection and credential typing."""
        d = Gtk.Dialog(title="Protocols", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        d.set_default_size(760, 620)
        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        head = Gtk.Label(xalign=0)
        head.set_markup(
            "A protocol is a button, one or more commands, and optionally a "
            "credential prompt to type into.\n"
            "Placeholders: <tt>{host} {port} {user} {password} {name} {file} "
            "{exe}</tt>")
        box.pack_start(head, False, False, 0)

        nb = Gtk.Notebook()
        nb.set_scrollable(True)
        box.pack_start(nb, True, True, 0)

        status = Gtk.Label(xalign=0)
        status.get_style_context().add_class("dim-label")
        box.pack_start(status, False, False, 0)

        pages: dict[str, dict] = {}

        def add_page(pr):
            grid = Gtk.Grid(row_spacing=5, column_spacing=8, border_width=10)
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            sw.add(grid)
            fields, r = {}, 0

            def entry(key, label, value, width=44, hint=""):
                nonlocal r
                grid.attach(Gtk.Label(label=label, xalign=1), 0, r, 1, 1)
                e = Gtk.Entry(text=str(value))
                e.set_width_chars(width)
                e.set_hexpand(True)
                if hint:
                    e.set_placeholder_text(hint)
                grid.attach(e, 1, r, 2, 1)
                fields[key] = e
                r += 1
                return e

            entry("label", "label", pr.label, 20)
            entry("color", "colour", pr.color, 20, "#rrggbb or accent")
            entry("order", "order", pr.order, 8)
            entry("port", "port", pr.port or "", 8, "blank = none")
            e_exe = entry("exe", "exe", pr.exe, 44, "optional; used as {exe}")
            det = Gtk.Button(label="Detect")
            grid.attach(det, 3, r - 1, 1, 1)
            entry("env", "env", " ".join(f"{k}={v}" for k, v in pr.env.items()),
                  44, "NAME=value NAME2=value2")

            # launchers
            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>Commands</b>")
            lbl.set_margin_top(6)
            grid.attach(lbl, 0, r, 3, 1)
            r += 1
            lrows: list[tuple] = []

            def add_launcher(name="", cmd=""):
                nonlocal r
                e_n = Gtk.Entry(text=name)
                e_n.set_width_chars(12)
                e_c = Gtk.Entry(text=cmd)
                e_c.set_width_chars(40)
                e_c.set_hexpand(True)
                xb = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                                   Gtk.IconSize.BUTTON)
                xb.set_relief(Gtk.ReliefStyle.NONE)
                row_widgets = (e_n, e_c, xb)
                grid.attach(e_n, 0, r, 1, 1)
                grid.attach(e_c, 1, r, 2, 1)
                grid.attach(xb, 3, r, 1, 1)

                def drop(*_a):
                    for w in row_widgets:
                        w.destroy()
                    lrows[:] = [t for t in lrows if t[0] is not e_n]
                xb.connect("clicked", drop)
                lrows.append((e_n, e_c))
                r += 1
                grid.show_all()

            for name, cmd in pr.launchers.items():
                add_launcher(name, cmd)
            addb = Gtk.Button(label="Add command")
            grid.attach(addb, 0, r, 2, 1)
            addb.connect("clicked", lambda *_a: add_launcher())
            r += 1
            entry("default", "default", pr.default, 20, "one of the names above")

            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>Session detection</b>")
            lbl.set_margin_top(6)
            grid.attach(lbl, 0, r, 3, 1)
            r += 1
            entry("detect.process", "process regex", pr.detect_process, 44)
            entry("detect.window", "window title", pr.detect_window, 44, "{host}")

            lbl = Gtk.Label(xalign=0)
            lbl.set_markup("<b>Credential typing</b>  "
                           "<small>(leave steps empty if the client handles auth)"
                           "</small>")
            lbl.set_margin_top(6)
            grid.attach(lbl, 0, r, 3, 1)
            r += 1
            entry("inject.window_class", "window class", pr.inject_class, 24)
            entry("inject.window_title", "window title", pr.inject_title, 24)
            entry("inject.wait", "wait (s)", pr.inject_wait, 8)
            entry("inject.settle", "settle (s)", pr.inject_settle, 8)
            entry("inject.delay", "key delay (ms)", pr.inject_delay, 8)
            entry("inject.steps", "steps",
                  " | ".join(op if not arg else f"{op}:{arg}"
                             for op, arg in pr.inject_steps), 44,
                  "type:{user} | key:Tab | type:{password} | key:Return")
            for extra_k, extra_v in pr.extras.items():
                entry(f"extra:{extra_k}", extra_k, extra_v, 44)

            rm = Gtk.Button(label="Remove this protocol")
            rm.get_style_context().add_class("destructive-action")
            rm.set_margin_top(10)
            grid.attach(rm, 0, r, 2, 1)

            def do_detect(*_a):
                pr2 = rcm.Protocol(id=pr.id, exe=fields["exe"].get_text().strip())
                found = rcm.protocol_detect(pr2)
                if not found:
                    status.set_markup("<small>nothing found</small>")
                    return
                fields["exe"].set_text(str(found[0]))
                parts = found[0].parts
                if "drive_c" in parts:
                    prefix = Path(*parts[:parts.index("drive_c")])
                    fields["env"].set_text(f"WINEPREFIX={prefix}")
                status.set_markup(f"<small>found {len(found)}; using {found[0]}</small>")
            det.connect("clicked", do_detect)

            def do_remove(*_a):
                pages.pop(pr.id, None)
                nb.remove_page(nb.page_num(sw))
                status.set_markup(f"<small>{pr.id} removed — Save to apply</small>")
            rm.connect("clicked", do_remove)

            nb.append_page(sw, Gtk.Label(label=pr.label or pr.id))
            pages[pr.id] = {"fields": fields, "launchers": lrows, "page": sw}
            nb.show_all()

        for pr in protocols_safe().values():
            add_page(pr)

        bar = Gtk.Box(spacing=6)
        box.pack_start(bar, False, False, 0)
        newb = Gtk.Button(label="Add protocol")
        bar.pack_start(newb, False, False, 0)
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        bar.pack_end(save, False, False, 0)

        def do_new(*_a):
            name = self._ask_text("New protocol", "Protocol id (lowercase, no spaces):",
                                  "anydesk")
            pid = re.sub(r"[^a-z0-9_-]+", "", name.lower())
            if not pid:
                return
            if pid in pages:
                status.set_markup(f"<small>{pid} already exists</small>")
                return
            add_page(rcm.Protocol(id=pid, label=pid.title(), color="#777777",
                                  order=max((int(v["fields"]["order"].get_text() or 0)
                                             for v in pages.values()), default=0) + 10))
            nb.set_current_page(nb.get_n_pages() - 1)
        newb.connect("clicked", do_new)

        def do_save(*_a):
            out: dict[str, rcm.Protocol] = {}
            for pid, data in pages.items():
                f = data["fields"]
                pr = rcm.Protocol(id=pid, label=f["label"].get_text().strip() or pid)
                pr.color = f["color"].get_text().strip() or "accent"
                for key, cast, attr in (("order", int, "order"), ("port", int, "port"),
                                        ("inject.wait", float, "inject_wait"),
                                        ("inject.settle", float, "inject_settle"),
                                        ("inject.delay", int, "inject_delay")):
                    raw = f[key].get_text().strip()
                    if raw:
                        try:
                            setattr(pr, attr, cast(raw))
                        except ValueError:
                            status.set_markup(
                                f"<small>{pid}: {key}={raw!r} is not a number</small>")
                            return
                pr.exe = f["exe"].get_text().strip()
                for pair in f["env"].get_text().split():
                    k, _, v = pair.partition("=")
                    if k:
                        pr.env[k] = v
                pr.launchers = {n.get_text().strip(): c.get_text().strip()
                                for n, c in data["launchers"]
                                if n.get_text().strip() and c.get_text().strip()}
                pr.default = f["default"].get_text().strip()
                if pr.default and pr.default not in pr.launchers:
                    status.set_markup(f"<small>{pid}: default "
                                      f"{pr.default!r} is not one of its commands</small>")
                    return
                pr.detect_process = f["detect.process"].get_text().strip()
                pr.detect_window = f["detect.window"].get_text().strip() or "{host}"
                pr.inject_class = f["inject.window_class"].get_text().strip()
                pr.inject_title = f["inject.window_title"].get_text().strip()
                steps = f["inject.steps"].get_text().strip()
                if steps:
                    try:
                        pr.inject_steps = rcm.parse_steps(steps)
                    except rcm.ConfigError as e:
                        status.set_markup(
                            f"<small>{GLib.markup_escape_text(str(e))}</small>")
                        return
                for key, ent in f.items():
                    if key.startswith("extra:") and ent.get_text().strip():
                        pr.extras[key[6:]] = ent.get_text().strip()
                out[pid] = pr
            if not out:
                status.set_markup("<small>at least one protocol is required</small>")
                return
            rcm.save_protocols(dict(sorted(out.items(),
                                           key=lambda kv: kv[1].order)))
            install_css(protocols_safe())
            self.rebuild_buttons()
            self.reload()
            status.set_markup("<small>saved</small>")
            self.say(f"protocols saved to {rcm.PROTOCOLS_FILE}")
        save.connect("clicked", do_save)

        d.show_all()
        d.run()
        d.destroy()

    # ---- shortcuts ----------------------------------------------------------- #
    def on_shortcuts(self, *_):
        d = Gtk.Dialog(title="Keyboard shortcuts", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        d.set_default_size(520, -1)
        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        head = Gtk.Label(xalign=0)
        head.set_markup("Click a shortcut, then press the keys you want.\n"
                        "<b>Backspace</b> clears it, <b>Escape</b> cancels.")
        box.pack_start(head, False, False, 0)

        g, s = rcm.load_shortcuts()
        buttons: dict[str, ShortcutButton] = {}
        grid = Gtk.Grid(row_spacing=6, column_spacing=10)
        box.pack_start(grid, False, False, 0)

        r = 0
        lb = Gtk.Label(xalign=0)
        lb.set_markup("<b>General</b>")
        grid.attach(lb, 0, r, 2, 1)
        r += 1
        for action, desc in rcm.GLOBAL_ACTIONS:
            grid.attach(Gtk.Label(label=desc, xalign=0), 0, r, 1, 1)
            b = ShortcutButton(g.get(action, ""))
            buttons[f"g:{action}"] = b
            grid.attach(b, 1, r, 1, 1)
            r += 1

        lb = Gtk.Label(xalign=0)
        lb.set_markup("<b>Per connection</b>")
        lb.set_margin_top(8)
        grid.attach(lb, 0, r, 2, 1)
        r += 1
        # Only connections that actually have a shortcut. New ones are assigned
        # from the connection's own Edit dialog.
        bound = [c for c in rcm.conns_cached() if s.get(c.sel)]
        if not bound:
            none_lbl = Gtk.Label(xalign=0)
            none_lbl.set_markup("<small>None yet — set one from a connection's "
                                "Edit dialog.</small>")
            none_lbl.get_style_context().add_class("dim-label")
            grid.attach(none_lbl, 0, r, 2, 1)
            r += 1
        for c in bound:
            grid.attach(Gtk.Label(label=c.sel, xalign=0), 0, r, 1, 1)
            b = ShortcutButton(s.get(c.sel, ""))
            buttons[f"s:{c.sel}"] = b
            grid.attach(b, 1, r, 1, 1)
            xb = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                               Gtk.IconSize.BUTTON)
            xb.set_tooltip_text(f"Remove the shortcut for {c.sel}")
            xb.set_relief(Gtk.ReliefStyle.NONE)
            xb.connect("clicked", lambda _w, sel=c.sel: drop_binding(sel))
            grid.attach(xb, 2, r, 1, 1)
            r += 1

        warn = Gtk.Label(xalign=0)
        warn.get_style_context().add_class("dim-label")
        box.pack_start(warn, False, False, 0)

        row = Gtk.Box(spacing=6)
        box.pack_start(row, False, False, 0)
        save = Gtk.Button(label="Save and apply")
        save.get_style_context().add_class("suggested-action")
        row.pack_start(save, False, False, 0)
        rm = Gtk.Button(label="Remove all from Cinnamon")
        row.pack_end(rm, False, False, 0)

        def drop_binding(sel: str) -> None:
            # No confirmation by request: it is one click to set again.
            gg, ss = rcm.load_shortcuts()
            ss.pop(sel, None)
            rcm.save_shortcuts(gg, ss)
            self.install_shortcuts()
            self.say(f"shortcut removed for {sel}")
            self.reload()
            d.response(Gtk.ResponseType.APPLY)   # reopen so the list redraws

        def collect():
            gg = {a: buttons[f"g:{a}"].binding for a, _ in rcm.GLOBAL_ACTIONS}
            ss = dict(rcm.load_shortcuts()[1])
            for c in bound:
                b = buttons[f"s:{c.sel}"].binding
                if b:
                    ss[c.sel] = b
                else:
                    ss.pop(c.sel, None)
            return gg, ss

        def dupes(gg, ss) -> list[str]:
            seen: dict[str, str] = {}
            out = []
            for name, b in list(gg.items()) + list(ss.items()):
                if not b:
                    continue
                if b in seen:
                    out.append(f"{pretty_key(b)} used by both {seen[b]} and {name}")
                seen[b] = name
            return out

        def do_save(*_a):
            gg, ss = collect()
            clashes = dupes(gg, ss)
            if clashes:
                warn.set_markup("<small>" + "; ".join(clashes) + "</small>")
                return
            rcm.save_shortcuts(gg, ss)
            n, warnings = self.install_shortcuts()
            warn.set_markup("<small>" + ("; ".join(warnings) if warnings else "") +
                            "</small>")
            self.say(f"{n} shortcut(s) saved and registered with Cinnamon")
            self.reload()

        def do_remove(*_a):
            n = rcm.shortcuts_remove()
            self.say(f"{n} shortcut(s) removed from Cinnamon "
                     "(shortcuts.conf kept)")

        save.connect("clicked", do_save)
        rm.connect("clicked", do_remove)
        d.show_all()
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.APPLY:
            self.on_shortcuts()   # a row was removed; redraw the list

    # ---- credentials --------------------------------------------------------- #
    def on_credentials(self, *_):
        d = Gtk.Dialog(title="Credentials", transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        d.set_default_size(560, -1)
        box = d.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)

        head = Gtk.Label(xalign=0)
        head.set_markup(
            "Passwords are kept in the <b>login keyring</b>, not on disk.\n"
            "Most specific wins, and within a scope a protocol-specific entry beats "
            "the generic one:\n"
            "<tt>Group/Host@radmin → Group/Host → Group@radmin → Group "
            "→ Default@radmin → Default</tt>")
        box.pack_start(head, False, False, 0)
        if not rcm.keyring_available():
            warn = Gtk.Label(xalign=0)
            warn.set_markup("<b>No keyring available</b> — passwords cannot be stored.")
            box.pack_start(warn, False, False, 0)

        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        box.pack_start(grid, True, True, 0)
        for i, title in enumerate(("Scope", "Username", "Password", "")):
            lb = Gtk.Label(xalign=0)
            lb.set_markup(f"<b>{title}</b>")
            grid.attach(lb, i, 0, 1, 1)

        rows: list[tuple[str, Gtk.Entry, Gtk.Entry, Gtk.Label]] = []

        def add_row(key: str, r: int) -> None:
            user, in_ring, in_file = rcm.creds_status(key)
            name = Gtk.Label(label="Default" if key == "DEFAULT" else key, xalign=0)
            if key == "DEFAULT" or "/" not in key:
                name.set_tooltip_text("Group scope" if key != "DEFAULT" else
                                      "Used when nothing more specific matches")
            grid.attach(name, 0, r, 1, 1)

            eu = Gtk.Entry(text=user, placeholder_text="(from the .rdp)")
            eu.set_width_chars(14)
            grid.attach(eu, 1, r, 1, 1)

            ep = Gtk.Entry(visibility=False)
            ep.set_width_chars(14)
            ep.set_placeholder_text("keep" if in_ring else "not set")
            grid.attach(ep, 2, r, 1, 1)

            state = Gtk.Label(xalign=0)
            state.set_markup("<small>in keyring</small>" if in_ring else
                             "<small>PLAINTEXT</small>" if in_file else
                             "<small>—</small>")
            grid.attach(state, 3, r, 1, 1)
            rows.append((key, eu, ep, state))

        scopes = rcm.creds_scopes()
        for r, key in enumerate(scopes, start=1):
            add_row(key, r)

        # Add a protocol-specific entry for an existing scope.
        addbar = Gtk.Box(spacing=6)
        addbar.set_margin_top(6)
        box.pack_start(addbar, False, False, 0)
        addbar.pack_start(Gtk.Label(label="Add for protocol:"), False, False, 0)
        scope_combo = Gtk.ComboBoxText()
        base_scopes = ["DEFAULT"] + sorted({c.group for c in rcm.conns_cached() if c.group})
        for sc in base_scopes:
            scope_combo.append_text(sc)
        scope_combo.set_active(0)
        addbar.pack_start(scope_combo, False, False, 0)
        proto_combo = Gtk.ComboBoxText()
        for pid in protocols_safe():
            proto_combo.append_text(pid)
        proto_combo.set_active(0)
        addbar.pack_start(proto_combo, False, False, 0)
        addb = Gtk.Button(label="Add")
        addbar.pack_start(addb, False, False, 0)

        def do_add(*_a):
            key = f"{scope_combo.get_active_text()}@{proto_combo.get_active_text()}"
            if any(k == key for k, _e, _p, _s in rows):
                self.say(f"{key} is already listed")
                return
            rcm.creds_set_user(key, "")
            self.say(f"added {key}")
            d.response(Gtk.ResponseType.APPLY)
        addb.connect("clicked", do_add)

        note = Gtk.Label(xalign=0)
        note.set_markup("<small>Leave a password blank to keep what is stored. "
                        "A single connection's override is set from its Edit "
                        "dialog.</small>")
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)

        btns = Gtk.Box(spacing=6)
        box.pack_start(btns, False, False, 0)
        save = Gtk.Button(label="Save")
        save.get_style_context().add_class("suggested-action")
        btns.pack_start(save, False, False, 0)
        clear = Gtk.Button(label="Clear password for selected scope…")
        btns.pack_start(clear, False, False, 0)
        combo = Gtk.ComboBoxText()
        for key in scopes:
            combo.append_text("Default" if key == "DEFAULT" else key)
        combo.set_active(0)
        btns.pack_end(combo, False, False, 0)

        def do_save(*_a):
            saved = 0
            for key, eu, ep, state in rows:
                rcm.creds_set_user(key, eu.get_text().strip())
                pw = ep.get_text()
                if pw:
                    if rcm.secret_set(key, pw):
                        saved += 1
                        ep.set_text("")
                        ep.set_placeholder_text("keep")
                        state.set_markup("<small>in keyring</small>")
            self.say(f"credentials saved ({saved} password(s) written)")

        def do_clear(*_a):
            idx = combo.get_active()
            if idx < 0:
                return
            key = scopes[idx]
            rcm.secret_clear(key)
            for k, _eu, ep, state in rows:
                if k == key:
                    ep.set_text("")
                    ep.set_placeholder_text("not set")
                    state.set_markup("<small>—</small>")
            self.say(f"cleared password for {key}")

        save.connect("clicked", do_save)
        clear.connect("clicked", do_clear)
        d.show_all()
        resp = d.run()
        d.destroy()
        if resp == Gtk.ResponseType.APPLY:
            self.on_credentials()   # a scope was added; redraw the list


def set_window_icon() -> None:
    """Icon for the window and the task switcher.

    Prefer the theme name so it follows any later reinstall; fall back to the
    file in the repo so a clone that has not run `rcm install-icon` still shows
    the right icon.
    """
    if Gtk.IconTheme.get_default().has_icon(rcm.ICON_NAME):
        Gtk.Window.set_default_icon_name(rcm.ICON_NAME)
        return
    if rcm.ICON_SVG.is_file():
        try:
            Gtk.Window.set_default_icon_from_file(str(rcm.ICON_SVG))
        except Exception:
            pass


def run() -> int:
    # The GUI is the one place allowed to create config: opening the manager is a
    # deliberate setup step, so a fresh clone gets real editable files rather
    # than invisible in-code defaults.
    set_window_icon()
    migrated = rcm.migrate_launchers()
    created = rcm.write_default_configs()
    w = Win()
    install_css(protocols_safe(), w)
    w.reload()
    # The offline guide regenerates whenever the registry grew or the code moved.
    try:
        rcm.dump_help_html()
    except OSError:
        pass
    if migrated:
        w.say("launchers.conf upgraded to protocols.conf")
    elif created:
        w.say("created " + ", ".join(created) + " — see Protocols… to finish setup")
    w.connect("destroy", Gtk.main_quit)
    w.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
