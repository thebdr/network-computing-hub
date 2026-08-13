#!/usr/bin/env python3
"""GTK3 manager window for rcm: saved connections on the left, live sessions right."""
from __future__ import annotations

import re
import shlex
import shutil
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import rcm  # noqa: E402
from rcm_help import help_topic as help_topic_gui  # noqa: E402

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
        "rail": "group sidebar · flat list · active pane",
        "spotlight": "one big filter over everything",
        "cockpit": "live sessions first, list below",
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
        setup_row = Gtk.ModelButton(label="Setup…")
        setup_row.connect("clicked", lambda *_: (popover.popdown(), self.on_setup()))
        box.pack_start(Gtk.Separator(), False, False, 4)
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
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and \
                Gdk.keyval_name(event.keyval) in ("t", "T"):
            self.pin_current_query()
            return True
        return False

    def connection_matches_query(self, c) -> bool:
        if self.query_group and not (c.group == self.query_group or
                                     c.group.startswith(self.query_group + "/")):
            return False
        if self.query_text:
            haystack = " ".join([c.sel, c.host, c.username]).lower()
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
                     "cockpit_cards"):
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
        self.flat_list = layout_id in ("rail", "spotlight", "cockpit")
        self.highlight_matches = layout_id == "spotlight"
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

    # ---- Rail ----------------------------------------------------------- #
    def build_rail_layout(self):
        """Design 8a: group sidebar, flat list, active pane."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(190)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_border_width(6)
        self.sidebar = Gtk.TreeView(headers_visible=False)
        self.sidebar.get_style_context().add_class("sidebar")
        self.sidebar_store = Gtk.TreeStore(str, str)   # markup, group path
        self.sidebar.set_model(self.sidebar_store)
        renderer = Gtk.CellRendererText()
        self.sidebar.append_column(Gtk.TreeViewColumn("", renderer, markup=0))
        self.sidebar.get_selection().connect("changed", self.on_sidebar_selected)
        side_scroller = Gtk.ScrolledWindow()
        side_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroller.add(self.sidebar)
        side.pack_start(side_scroller, True, True, 0)
        side.pack_start(Gtk.Separator(), False, False, 0)
        self.setup_badge = Gtk.Button()
        self.setup_badge.set_relief(Gtk.ReliefStyle.NONE)
        self.setup_badge.connect("clicked", lambda *_: self.on_setup())
        side.pack_start(self.setup_badge, False, False, 0)
        paned.pack1(side, False, False)

        right = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        right.set_position(600)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main.set_border_width(8)
        main.pack_start(self.build_filter_row(
            "filter name, host, user…  (Ctrl+T pins as tab)"), False, False, 0)
        main.pack_start(self.build_connection_list(), True, True, 0)
        main.pack_start(self.build_connect_button_box(), False, False, 0)
        right.pack1(main, True, False)
        right.pack2(self.build_active_pane(), False, False)
        paned.pack2(right, True, False)
        return paned

    def on_sidebar_selected(self, selection) -> None:
        model, it = selection.get_selected()
        if it:
            self.query_group = model[it][1]
            self.reload()

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
        self.sidebar.expand_all()
        problems = len(rcm.config_health())
        self.setup_badge.set_label(
            f"⚙ Setup{f'  ({problems}⚠)' if problems else ''}")

    @help_topic_gui("setup-page", "The Setup page",
                    ("setup", "settings", "warnings", "import", "export"),
                    section="Setup")
    def on_setup(self) -> None:
        """Everything that used to hide in the menu lives on the Setup page.

        Four cards — Protocols, Credentials, Keyboard shortcuts, Import/Export
        — each fronting the same plain files you can edit by hand. Warnings
        from the health check appear as a banner with a one-click action, and
        the badge on the Setup entry counts them.
        """
        self.reset_body_references()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)

        bar = Gtk.Box(spacing=8)
        back = Gtk.Button(label="‹ Back")
        back.connect("clicked", lambda *_: self.apply_layout(self.ui_state["layout"]))
        bar.pack_start(back, False, False, 0)
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Setup</b>")
        bar.pack_start(heading, False, False, 0)
        page.pack_start(bar, False, False, 0)

        problems = rcm.config_health()
        if problems:
            banner = Gtk.Frame()
            inner = Gtk.Box(spacing=8)
            inner.set_border_width(8)
            text = Gtk.Label(xalign=0)
            text.set_markup(f'<span foreground="#b5890a">⚠ '
                            f'{GLib.markup_escape_text(problems[0].message)}</span>')
            text.set_line_wrap(True)
            inner.pack_start(text, True, True, 0)
            if problems[0].setup_section == "credentials":
                act = Gtk.Button(label="Set default credentials")
                act.get_style_context().add_class("suggested-action")
                act.connect("clicked", lambda *_: self.set_default_credentials())
                inner.pack_end(act, False, False, 0)
            banner.add(inner)
            page.pack_start(banner, False, False, 0)

        cards = Gtk.Grid(row_spacing=10, column_spacing=10,
                         row_homogeneous=True, column_homogeneous=True)
        page.pack_start(cards, True, True, 0)

        def card(title, subtitle, extra_widget, buttons):
            frame = Gtk.Frame()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.set_border_width(10)
            head = Gtk.Label(xalign=0)
            head.set_markup(f"<b>{title}</b>")
            box.pack_start(head, False, False, 0)
            sub = Gtk.Label(xalign=0, label=subtitle)
            sub.set_line_wrap(True)
            sub.get_style_context().add_class("dim-label")
            box.pack_start(sub, False, False, 0)
            if extra_widget is not None:
                box.pack_start(extra_widget, False, False, 0)
            row = Gtk.Box(spacing=6)
            for label, handler, suggested in buttons:
                b = Gtk.Button(label=label)
                if suggested:
                    b.get_style_context().add_class("suggested-action")
                b.connect("clicked", lambda _w, h=handler: h())
                row.pack_start(b, False, False, 0)
            box.pack_start(row, False, False, 0)
            frame.add(box)
            return frame

        chips = Gtk.Label(xalign=0)
        chips.set_markup(protocol_badges(list(protocols_safe()),
                                         protocols_safe()))
        cards.attach(card("Protocols",
                          "buttons, commands, session detection, credential typing",
                          chips,
                          [("Edit…", self.on_protocols, False),
                           ("Add protocol", self.on_protocols, False)]), 0, 0, 1, 1)

        scopes = ", ".join(k for k in rcm.creds_scopes() if k != "DEFAULT") or "—"
        missing_default = any(w.warning_id == "no-default-credential"
                              for w in problems)
        cards.attach(card("Credentials" + ("  ⚠ no DEFAULT" if missing_default else ""),
                          "usernames in creds.conf, passwords in the OS keyring "
                          "— never on disk",
                          Gtk.Label(xalign=0, label=f"scopes: {scopes}"),
                          [("Set default…", self.set_default_credentials,
                            missing_default),
                           ("Manage…", self.on_credentials, False)]), 1, 0, 1, 1)

        globals_, per_session = rcm.load_shortcuts()
        summary = " · ".join(f"{pretty_key(v)} {k}" for k, v in globals_.items()
                             if v) or "none configured"
        cards.attach(card("Keyboard shortcuts",
                          f"{summary} · {len(per_session)} connection key(s)",
                          None,
                          [("Edit…", self.on_shortcuts, False),
                           ("Reinstall", lambda: (rcm.shortcuts_install(),
                                                  self.say("shortcuts reinstalled"))[1],
                            False)]), 0, 1, 1, 1)

        cards.attach(card("Import / Export",
                          "CSV round-trips everything; .rdp files and Radmin "
                          "phonebook export",
                          None,
                          [("Import CSV…", self.on_import, False),
                           ("Export…", self.on_export, False),
                           ("Phonebook…", self.on_phonebook, False)]), 1, 1, 1, 1)

        foot = Gtk.Label(xalign=0)
        foot.set_markup("<small>everything here is a plain file — protocols.conf, "
                        "creds.conf, shortcuts.conf — this page is a convenience "
                        "over the same files</small>")
        foot.get_style_context().add_class("dim-label")
        page.pack_start(foot, False, False, 0)
        self.layout_container.pack_start(page, True, True, 0)
        self.layout_container.show_all()

    def set_default_credentials(self) -> None:
        """The DEFAULT credential backs every connection with no closer match."""
        d = Gtk.Dialog(title="Set default credentials", transient_for=self,
                       modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        box = d.get_content_area()
        box.set_spacing(6)
        box.set_border_width(12)
        box.add(Gtk.Label(xalign=0, label="Username (blank = from each .rdp):"))
        user_entry = Gtk.Entry(activates_default=True)
        box.add(user_entry)
        box.add(Gtk.Label(xalign=0, label="Password (stored in the OS keyring):"))
        pw_entry = Gtk.Entry(visibility=False, activates_default=True)
        box.add(pw_entry)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        response = d.run()
        username, password = user_entry.get_text().strip(), pw_entry.get_text()
        d.destroy()
        if response != Gtk.ResponseType.OK:
            return
        rcm.creds_set_user("DEFAULT", username)
        if password:
            rcm.secret_set("DEFAULT", password)
        self.say("DEFAULT credentials saved")
        self.on_setup()

    @help_topic_gui("first-run", "First-run checklist",
                    ("empty", "getting started", "checklist"), section="Setup")
    def build_first_run(self):
        """With no connections yet, the window opens as a setup checklist.

        Each row is a step with its done-state checked live: detect installed
        clients, install the keyboard shortcuts, set default credentials,
        create the first connection, or import a CSV. Skipping shows the
        normal (empty) window.
        """
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(30)
        title = Gtk.Label()
        title.set_markup("<span size='x-large'><b>Set up your hub</b></span>")
        page.pack_start(title, False, False, 4)

        registry = protocols_safe()
        any_launcher = any(
            rcm.launcher_exe(proto, tmpl) and
            (shutil.which(rcm.launcher_exe(proto, tmpl)) or
             Path(rcm.launcher_exe(proto, tmpl)).is_file())
            for proto in registry.values() for tmpl in proto.launchers.values())
        shortcuts_installed = bool(rcm.parse_gsettings_list(
            rcm.run_gsettings("get", rcm.GS_LIST, "custom-list")))
        have_default = bool(rcm.secret_get("DEFAULT"))
        steps = [
            ("Detect installed clients", any_launcher, self.on_protocols, False),
            ("Install keyboard shortcuts", shortcuts_installed,
             lambda: (rcm.shortcuts_install(), self.reload()), False),
            ("Set default credentials", have_default,
             self.set_default_credentials, False),
            ("Create first connection", False,
             lambda: self.edit_dialog(None), True),
            ("Import CSV", False, self.on_import, False),
        ]
        done = sum(1 for _t, state, _h, _s in steps if state)
        progress = Gtk.Label()
        progress.set_markup(f"<small>{done} of {len(steps)} done</small>")
        progress.get_style_context().add_class("dim-label")
        page.pack_start(progress, False, False, 0)

        for text, done_state, handler, suggested in steps:
            row = Gtk.Box(spacing=10)
            mark = Gtk.Label(label="✓" if done_state else "○")
            row.pack_start(mark, False, False, 0)
            row.pack_start(Gtk.Label(xalign=0, label=text), True, True, 0)
            button = Gtk.Button(label="Done" if done_state else "Go")
            button.set_sensitive(not done_state)
            if suggested and not done_state:
                button.get_style_context().add_class("suggested-action")
            button.connect("clicked", lambda _w, h=handler: h())
            row.pack_end(button, False, False, 0)
            page.pack_start(row, False, False, 2)

        skip = Gtk.Button(label="Skip — show me the empty list")
        skip.set_relief(Gtk.ReliefStyle.NONE)
        skip.connect("clicked", lambda *_: (setattr(self, "first_run_skipped", True),
                                            self.apply_layout(self.ui_state["layout"])))
        page.pack_start(skip, False, False, 8)
        return page

    # ---- Spotlight ------------------------------------------------------ #
    def build_spotlight_layout(self):
        """Design 6a: the filter is the whole navigation."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(
            "matches name · host · group · user")
        self.filter_entry.set_text(self.query_text)
        # 15px query text per the design tokens.
        self.filter_entry.modify_font(Pango.FontDescription("15"))
        self.filter_entry.connect("search-changed", self.on_filter_changed)
        box.pack_start(self.filter_entry, False, False, 0)

        chips = Gtk.Box(spacing=6)
        self.live_chip = Gtk.ToggleButton(label="Live")
        self.live_chip.connect("toggled", lambda *_: self.reload())
        chips.pack_start(self.live_chip, False, False, 0)
        self.proto_chips = {}
        for proto in protocols_safe().values():
            chip = Gtk.ToggleButton(label=proto.label)
            chip.connect("toggled", lambda *_: self.reload())
            self.proto_chips[proto.id] = chip
            chips.pack_start(chip, False, False, 0)
        box.pack_start(chips, False, False, 0)
        box.pack_start(self.build_connection_list(), True, True, 0)
        box.pack_start(self.build_connect_button_box(), False, False, 0)
        return box

    def spotlight_row_allowed(self, c) -> bool:
        if getattr(self, "live_chip", None) and self.live_chip.get_active():
            if c.sel not in rcm.active_sels():
                return False
        chips = getattr(self, "proto_chips", None) or {}
        wanted = [pid for pid, chip in chips.items() if chip.get_active()]
        if wanted and not any(pid in c.protocols for pid in wanted):
            return False
        return True

    # ---- Cockpit -------------------------------------------------------- #
    def build_cockpit_layout(self):
        """Design 6b: live sessions as the headline strip."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        strip_label = Gtk.Label(xalign=0)
        strip_label.set_markup("<b>Active</b>")
        box.pack_start(strip_label, False, False, 0)
        self.cockpit_cards = Gtk.FlowBox()
        self.cockpit_cards.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cockpit_cards.set_max_children_per_line(6)
        self.cockpit_cards.set_min_children_per_line(2)
        box.pack_start(self.cockpit_cards, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)
        box.pack_start(self.build_filter_row("filter…"), False, False, 0)
        box.pack_start(self.build_connection_list(), True, True, 0)
        box.pack_start(self.build_connect_button_box(), False, False, 0)
        return box

    def refresh_cockpit_cards(self) -> None:
        if getattr(self, "cockpit_cards", None) is None:
            return
        for child in self.cockpit_cards.get_children():
            child.destroy()
        registry = protocols_safe()
        keys = rcm.session_bindings()
        sessions = rcm.sessions()
        if not sessions:
            empty = Gtk.Label(label="no active sessions")
            empty.get_style_context().add_class("dim-label")
            self.cockpit_cards.add(empty)
        for session in sessions:
            card = Gtk.Frame()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            inner.set_border_width(8)
            head = Gtk.Label(xalign=0)
            proto = next((p for p in registry.values()
                          if p.label == session.proto), None)
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
                if self.current_layout == "spotlight" and \
                        not self.spotlight_row_allowed(c):
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
        if self.current_layout == "spotlight" and self.filter_entry:
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
                         lambda _m, pid=proto.id: self.connect_selected(pid, "", cs))
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
        automatically. Typing a new group name creates the group.

        Each configured protocol is a row: tick it to offer that protocol on
        this connection, pick one row as the Default (what double-click and
        the shortcut use — underlined in the list). A row's parameters show
        while it is ticked, and values are never lost by unticking: they are
        kept, shown as e.g. "port 5901 kept", and return on re-tick.

        The password override and the shortcut live in their own expanders;
        a blank password always means "leave as is".
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
            rcm.shortcuts_install()
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
            rcm.shortcuts_install()
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
            n, warnings = rcm.shortcuts_install()
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
