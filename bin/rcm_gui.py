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


def install_css(protos=None) -> None:
    prov = Gtk.CssProvider()
    prov.load_from_data(build_protocol_css(protos or {}))
    screen = Gdk.Screen.get_default()
    if screen:
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

# TreeStore columns
C_LABEL, C_HOST, C_USER, C_KEY, C_SEL, C_MARK, C_WEIGHT = range(7)

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
        self.set_default_size(980, 580)

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

        menu_btn = Gtk.MenuButton()
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        for label, cb in (("Credentials…", self.on_credentials),
                          ("Protocols…", self.on_protocols),
                          ("Keyboard shortcuts…", self.on_shortcuts),
                          (None, None),
                          ("Import from CSV…", self.on_import),
                          ("Export to CSV…", self.on_export),
                          ("Export Radmin phonebook…", self.on_phonebook),
                          (None, None),
                          ("Rebuild jump list", self.on_genlauncher)):
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", cb)
            menu.append(mi)
        menu.show_all()
        menu_btn.set_popup(menu)
        hb.pack_end(menu_btn)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(560)
        outer.pack_start(paned, True, True, 0)

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

        self.store = Gtk.TreeStore(str, str, str, str, str, str, int)
        self.tree = Gtk.TreeView(model=self.store)
        # Ctrl and Shift range-select come free with MULTIPLE.
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.set_rubber_banding(True)

        r = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("", r, text=C_MARK)
        col.set_min_width(24)
        self.tree.append_column(col)
        for idx, title, minw in ((C_LABEL, "Connection", 140),
                                 (C_HOST, "Host", 125), (C_USER, "User", 85),
                                 (C_KEY, "Key", 95)):
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=idx)
            col.add_attribute(r, "weight", C_WEIGHT)   # bold = a session is live
            col.set_resizable(True)
            col.set_min_width(minw)
            col.set_expand(idx == C_LABEL)
            self.tree.append_column(col)

        self.tree.connect("row-activated", lambda *_: self.connect_selected("rdp"))
        self.tree.connect("button-press-event", self.on_tree_click)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        left.pack_start(sw, True, True, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_markup("<small>Ctrl/Shift for multiple · select a group to take all "
                        "of it · right-click for other programs</small>")
        hint.get_style_context().add_class("dim-label")
        left.pack_start(hint, False, False, 0)

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

        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.set_margin_start(10)
        self.status.set_margin_end(10)
        self.status.set_margin_bottom(6)
        outer.pack_start(self.status, False, False, 0)

        self.reload()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

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
        keys = rcm.session_bindings()
        self.store.clear()
        parents: dict[str, Gtk.TreeIter] = {}
        for c in rcm.conns_cached():
            it = None
            if c.group:
                if c.group not in parents:
                    # Field order must match C_LABEL, C_HOST, C_USER, C_KEY,
                    # C_SEL, C_MARK, C_WEIGHT -- not the visual column order.
                    parents[c.group] = self.store.append(
                        None, [c.group, "", "", "", "", "", 400])
                it = parents[c.group]
            self.store.append(it, [c.name, c.host, c.username,
                                   pretty_key(keys.get(c.sel, "")), c.sel, "", 400])
        self.tree.expand_all()
        self.count_lbl.set_text(f"({len(rcm.conns_cached())})")
        self.refresh_live()

    def refresh_live(self) -> None:
        """Repaint the active-session list and the live markers in the tree."""
        live = rcm.active_sels()

        smodel, spaths = self.slist.get_selection().get_selected_rows()
        keep = {smodel[smodel.get_iter(p)][4] for p in spaths}
        keys = rcm.session_bindings()
        self.sstore.clear()
        for s in rcm.sessions():
            self.sstore.append([s.proto, s.label, s.host,
                                pretty_key(keys.get(s.label, "")), s.pid, s.window])
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
                    self.store[it][C_WEIGHT] = 700 if lit else 400
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
                group = model[it][C_LABEL]
                out.extend(c for c in rcm.conns_cached() if c.group == group)
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
    def connect_selected(self, proto: str, launcher: str = "") -> None:
        cs = self._selected_conns()
        if not cs:
            self.say("select a connection first")
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

    def on_tree_click(self, _w, event):
        if event.button != 3:
            return False
        info = self.tree.get_path_at_pos(int(event.x), int(event.y))
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
                top.connect("activate", lambda _m, pid=pr.id: self.connect_selected(pid))
            else:
                sub = Gtk.Menu()
                for nm in pr.launchers:
                    mi = Gtk.MenuItem(label=f"{nm}  (default)" if nm == pr.default else nm)
                    mi.connect("activate",
                               lambda _m, pid=pr.id, n=nm: self.connect_selected(pid, n))
                    sub.append(mi)
                top.set_submenu(sub)
            menu.append(top)

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
            for c in cs:
                rcm.delete_conn(c)
            rcm.gen_launcher()
            self.reload()
            self.say(f"deleted {len(cs)} connection(s)")

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

    def edit_dialog(self, c: rcm.Conn | None) -> None:
        new = c is None
        d = Gtk.Dialog(title="New connection" if new else f"Edit {c.sel}",
                       transient_for=self, modal=True)
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                      Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, border_width=12)
        d.get_content_area().add(grid)
        registry = protocols_safe()
        rows = [("Group", c.group if c else ""), ("Name", c.name if c else ""),
                ("Host", c.host if c else ""), ("Username", c.username if c else "")]
        # One port field per protocol that uses one, straight from the registry.
        port_ids = []
        for pr in registry.values():
            if not pr.port:
                continue
            port_ids.append(pr.id)
            rows.append((f"{pr.label} port",
                         str(c.port_for(pr.id)) if c else str(pr.port)))
        fields = {}
        for i, (label, val) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            e = Gtk.Entry(text=val, activates_default=True)
            e.set_width_chars(28)
            if not new and label in ("Group", "Name"):
                e.set_sensitive(False)  # the filename is the identity; rename = Duplicate
                e.set_tooltip_text("Use Duplicate to change the group or name")
            grid.attach(e, 1, i, 1, 1)
            fields[label] = e

        grid.attach(Gtk.Label(label="Protocols", xalign=1), 0, len(rows), 1, 1)
        pbox = Gtk.Box(spacing=10)
        checks = {}
        have = c.protocols if c else tuple(registry)
        for pr in registry.values():
            cb = Gtk.CheckButton(label=pr.label)
            cb.set_active(pr.id in have)
            checks[pr.id] = cb
            pbox.pack_start(cb, False, False, 0)
        grid.attach(pbox, 1, len(rows), 1, 1)

        # Per-connection credential override. Blank password = inherit the group's
        # entry, then Default. Only written when the user actually types one.
        r = len(rows) + 1
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(6)
        grid.attach(sep, 0, r, 2, 1)
        inherited_user, inherited_pw = ("", "")
        if c:
            inherited_user, inherited_pw = rcm.creds_lookup(c.sel, c.username)
        has_own = bool(c and rcm.secret_get(c.sel))
        lbl = Gtk.Label(xalign=1, label="Password")
        grid.attach(lbl, 0, r + 1, 1, 1)
        epw = Gtk.Entry(visibility=False, activates_default=True)
        epw.set_width_chars(28)
        epw.set_placeholder_text("override for this connection"
                                 if not has_own else "keep this connection's password")
        grid.attach(epw, 1, r + 1, 1, 1)
        info = Gtk.Label(xalign=0)
        info.set_markup(
            "<small>" + ("This connection has its own password." if has_own else
                         (f"Inherits a password (user {inherited_user})."
                          if inherited_pw else "No password set anywhere yet."))
            + " Blank = leave as is.</small>")
        info.get_style_context().add_class("dim-label")
        grid.attach(info, 1, r + 2, 1, 1)
        clear_pw = Gtk.CheckButton(label="Remove this connection's own password")
        clear_pw.set_sensitive(has_own)
        grid.attach(clear_pw, 1, r + 3, 1, 1)

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep2.set_margin_top(6)
        grid.attach(sep2, 0, r + 4, 2, 1)
        grid.attach(Gtk.Label(label="Shortcut", xalign=1), 0, r + 5, 1, 1)
        sc_box = Gtk.Box(spacing=6)
        cur_binding = rcm.session_bindings().get(c.sel, "") if c else ""
        sc_btn = ShortcutButton(cur_binding)
        sc_box.pack_start(sc_btn, False, False, 0)
        sc_hint = Gtk.Label(xalign=0)
        sc_hint.set_markup("<small>focuses it if live, connects it if not</small>")
        sc_hint.get_style_context().add_class("dim-label")
        sc_box.pack_start(sc_hint, False, False, 0)
        grid.attach(sc_box, 1, r + 5, 1, 1)

        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return
        g = fields["Group"].get_text().strip()
        n = fields["Name"].get_text().strip()
        h = fields["Host"].get_text().strip()
        u = fields["Username"].get_text().strip()

        def as_int(key, fallback):
            try:
                return int(fields[key].get_text().strip() or fallback)
            except ValueError:
                return fallback

        ports = {pid: as_int(f"{registry[pid].label} port", registry[pid].port)
                 for pid in port_ids}
        p = ports.pop("rdp", 3389)
        chosen = [k for k, cb in checks.items() if cb.get_active()] or list(registry)
        protos = "" if len(chosen) == len(registry) else ",".join(chosen)
        new_pw = epw.get_text()
        drop_pw = clear_pw.get_active()
        new_binding = sc_btn.binding
        d.destroy()

        if not n or not h:
            self._dialog(Gtk.MessageType.ERROR, "Name and Host are required")
            return
        if new:
            try:
                rcm.write_rdp(g, n, h, u, p, ports=ports, protocols=protos)
            except FileExistsError:
                self._dialog(Gtk.MessageType.ERROR, "That connection already exists")
                return
            self.say(f"created {g}/{n}" if g else f"created {n}")
        else:
            rcm.set_fields(c, host=h, username=u, port=p, ports=ports, protocols=protos)
            self.say(f"saved {c.sel}")

        sel = f"{g}/{n}" if g else n
        if drop_pw:
            rcm.secret_clear(sel)
            self.say(f"saved {sel}; its own password removed (now inherited)")
        elif new_pw:
            if rcm.secret_set(sel, new_pw):
                self.say(f"saved {sel} with its own password")
            else:
                self._dialog(Gtk.MessageType.ERROR, "Could not store the password",
                             "No keyring is available.")

        if new_binding != cur_binding:
            g, s = rcm.load_shortcuts()
            if new_binding:
                s[sel] = new_binding
            else:
                s.pop(sel, None)
            if c and c.sel != sel:
                s.pop(c.sel, None)
            rcm.save_shortcuts(g, s)
            rcm.shortcuts_install()
            self.say(f"saved {sel}; shortcut "
                     + (f"set to {pretty_key(new_binding)}" if new_binding else "removed"))

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


def run() -> int:
    # The GUI is the one place allowed to create config: opening the manager is a
    # deliberate setup step, so a fresh clone gets real editable files rather
    # than invisible in-code defaults.
    migrated = rcm.migrate_launchers()
    created = rcm.write_default_configs()
    install_css(protocols_safe())
    w = Win()
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
