# Network Computing Hub

One place to keep your remote connections, launched with whatever client you already
use, with credentials in the OS keyring instead of a text file.

The command is `rcm`.

It is a **manager and launcher**, not a protocol implementation. Sessions are opened by
external programs you configure — Thincast, `mstsc`, xfreerdp, Remmina, TigerVNC,
Radmin Viewer, AnyDesk, anything with a command line — so you keep the clients you like
and get grouping, batch import, live session tracking and global shortcuts on top.

**Protocols are configurable** RDP, VNC and Radmin ship as three sections in
`protocols.conf`; adding a fourth needs no changes to the program. A protocol defines its
own button label and colour, its commands, how a live session is recognised, and — if the
client does not provide a credentials management, automate the insertion on a password prompt via keystrokes.

- **Any protocol you can launch**, defined in config: label, colour, commands,
  session detection, and optional credential typing. 
- **Connections in one tree**, grouped, defined by ordinary `.rdp` files
- **Credentials in the OS keyring** (libsecret / GNOME Keyring), never on disk, with a
  precedence chain from per-connection down to a global default, and per-protocol
  entries for the same host.
- **Any client you have installed**, per protocol, configured as command lines with placeholders
- **Live session list** — see what is connected, focus it, disconnect it
- **System-wide shortcuts** and an Alt-Tab-style switcher across active sessions
- **Batch CSV import/export**, .rdp, and an experimental Radmin phonebook export
- **Tick connections** to queue them up for the Connect buttons; right-click any
  selection to export it as `.rdp` files, a CSV, or a Radmin phonebook

Linux/X11 (developed on Cinnamon). A Windows port is planned — see [Roadmap](#roadmap).

---

## Install

Requirements: Python 3.11+, PyGObject (GTK 3) and `xdotool`. Plus whichever clients
you want it to launch — none are required to install.

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 xdotool

git clone https://github.com/thebdr/network-computing-hub.git ~/network-computing-hub
cd ~/network-computing-hub
./bin/rcm init          # writes protocols.conf, creds.conf, shortcuts.conf
./bin/rcm install-icon  # app icon into ~/.local/share/icons
mkdir -p ~/bin && ln -s ~/network-computing-hub/bin/rcm ~/bin/rcm
```

Open the manager once (`rcm gui`) and it will create any missing config for you.

## Quick start

```bash
rcm init                       # create config files
rcm detect radmin --write      # find Radmin, if you use it
rcm import hosts.csv           # or add connections in the GUI
rcm gui                        # the manager
```

CSV columns are `group,name,host,username,port`, plus `port_<protocol>`, `protocols`,
`default_protocol` and `shortcut` — only `name` and `host` are required. A full export writes all of them,
so exporting and re-importing round-trips a connection unchanged. Imported connections
are generated from `templates/default.rdp`, so whatever you set there is inherited by
every host.

## How connections are stored

`RDP/<Group>/<Name>.rdp` — ordinary RDP files, which are also the single source of
truth for host and username. **Groups nest as deeply as you like**: put a connection in
`RDP/Plant/Line2/Cells/` and the tree follows the folders. Ticking or selecting a group
takes everything beneath it, at any depth. Two extra keys are ours; clients ignore keys they do not
recognise:

```
rcm-protocols:s:rdp,vnc,radmin
rcm-default-proto:s:rdp
rcm-port-vnc:i:5900
```

`rcm-default-proto` is what a double-click and that connection's shortcut use; it is
underlined in the Protocols column. Set it in the connection's Edit dialog.

`rcm-port-<protocol>` overrides that protocol's default port for one host, so a new
protocol needs no code change. RDP keeps using the file's own `server port`.

Rename a group folder with a leading dot (`Plant` → `.Plant`) and it disappears from
every menu, list and export.

## Configuration

Everything is a plain file you can edit by hand; the GUI is a convenience over the
same files, never the only way in. **Nothing is guessed in code** — if a value is
missing or wrong, the app says which file and which key, instead of silently falling
back to something that happened to work on the author's machine.

| File | What |
|---|---|
| `protocols.conf` | the protocols: buttons, commands, detection, credential typing |
| `creds.conf` | scope → username. **No passwords** — those are in the keyring |
| `shortcuts.conf` | system-wide keys |

### Protocols

Each `[protocol:<id>]` section is one way of reaching a machine:

```ini
[protocol:rdp]
label = RDP
color = accent                  ; or #rrggbb for the button colour
order = 10
port  = 3389
default = Thincast
launcher.Thincast = flatpak run com.thincast.client {file}
launcher.xfreerdp = xfreerdp3 /v:{host}:{port} /u:{user} +clipboard
detect.process = (^|/)(rdc|xfreerdp3?|mstsc)\b
```

Placeholders are substituted per argument, so paths with spaces stay one argument:
`{host} {port} {user} {password} {name} {file} {exe}`. The buttons use `default`;
right-click a connection for the others. Edit it all in the GUI under **Protocols…**,
which also writes new protocols for you.

`exe` is optional and available to commands as `{exe}`; `env.NAME = value` sets
environment for the launch. Together they cover a program that has to run through a
wrapper — Radmin under Wine is just:

```ini
[protocol:radmin]
exe = /path/to/Radmin.exe
env.WINEPREFIX = ~/.local/share/wineprefixes/Radmin
launcher.Wine = wine-stable {exe} /connect:{host}:{port}
launcher.Native = {exe} /connect:{host}:{port}
```

`rcm detect radmin` ranks the installs it can find; `--write` records the best one.

### Typing credentials into a prompt

Some clients refuse to take a password on the command line — Radmin has no
`/password:` switch because Famatech deliberately prevent saving passwords on the
Viewer machine. For those, describe the prompt and the keystrokes:

```ini
inject.window_class = [Rr]admin      ; X11 class of the prompt
inject.window_title = security       ; substring of its title
inject.wait   = 20                   ; seconds to wait for it
inject.settle = 0.3                  ; pause after focusing, before typing
inject.delay  = 40                   ; ms between keystrokes
inject.steps  = type:{user} | key:Tab | type:{password} | key:Return
```

Steps are `type:<text>`, `key:<keysym>`, `sleep:<seconds>`, `activate` and `clear`.
`{password}` is read from the keyring at the moment it is typed and never written to
disk. `rcm calibrate <connection> [protocol]` opens the prompt, reports exactly what it
is, saves a screenshot of it, and types nothing — use it when writing the steps.

### Hooks and via-gateways

Every connection can carry a `pre` and `post` command (**Edit ▸ Hooks & gateway**,
stored as `rcm-pre` / `rcm-post` in its file). `pre` runs first and must exit 0 or
the launch stops — mount a share, bring up a VPN, check a precondition; `post` fires
after the client starts. Both get the launcher placeholders.

A host that is only reachable through a bastion points at a gateway instead:

```ini
[via:plant-gw]         ; in protocols.conf
host = gw.example.local
user = maint
; port = 22            ; the gateway's SSH port
; hold = 30            ; seconds the tunnel waits for the client to attach
```

Pick it in **Edit ▸ Hooks & gateway** (stored as `rcm-via`). Connecting then opens
`ssh -f -L <free port>:<host>:<port>` through the gateway and hands the launcher
`127.0.0.1:<free port>` in `{host}`/`{port}` — any protocol, no client support
needed. The tunnel wants key auth to the gateway (it runs BatchMode, so it fails
loudly instead of sitting on an invisible prompt) and lives exactly as long as the
client's connection. The pre hook still sees the real `{host}`: it runs before the
tunnel opens. A dangling `rcm-via` shows up in config health.

### Credentials

Passwords live in the login keyring. `creds.conf` holds only `key|username|`. Lookup is
most-specific-first, and within a scope a protocol-specific entry beats the generic one:

```
Group/Host@radmin → Group/Host → Group@radmin → Group → DEFAULT@radmin → DEFAULT
```

Nested groups add a rung per level, so a password set on `Plant` covers everything
under it unless something deeper overrides:

```
Plant/Line2/Cells/Cell A → Plant/Line2/Cells → Plant/Line2 → Plant → DEFAULT
```

```bash
rcm creds list          # where each password lives, never the value
rcm creds set Group     # prompts without echo
rcm creds migrate       # move leftover plaintext into the keyring
```

## Selecting and exporting

Two independent mechanisms, on purpose:

- **Tick boxes** choose what the Connect buttons act on. Ticking a group ticks all of
  its members, and the tickbox in the column header ticks every *visible* row —
  ✓ when all are ticked, – when only some are — so filtering first and then
  ticking the header selects a whole group, tag or search result. (With nothing
  ticked the buttons fall back to the highlighted rows.)
- **Ctrl/Shift selection** chooses what the right-click menu acts on — connect with a
  specific program, edit, delete, or **Export ▸** the selection as `.rdp` files, a
  re-importable CSV, or a Radmin phonebook. A selected group means all of it.

**Ctrl+C / Ctrl+V** copy connections and whole groups (subtree included) and paste
them **inside** whichever group you are in — the sidebar selection, else the
highlighted row's group, else the top level. That one rule holds whatever is on the
clipboard and wherever you paste it, including a group pasted into itself (staged
through a temporary copy so it cannot walk into what it is writing). A pasted copy is
a byte copy of the `.rdp`, so tags, hooks, gateway and per-protocol ports come along;
names never collide, since an existing name becomes `<name> (copy)`, then `(copy 2)`.
Passwords are deliberately not copied: the keyring entry belongs to the original's
name, so the copy inherits from its group chain until you give it one of its own.

| Key | Action |
|---|---|
| `Ctrl+C` / `Ctrl+V` | copy the selection · paste inside the current group |
| `Ctrl+D` | duplicate the selection beside itself |
| `Ctrl+E` | edit the highlighted connection |
| `Delete` | delete the selection (also on a sidebar group) |

Right-clicking a group in the Browse sidebar copies, pastes into, **renames** or
deletes that whole group; groups fold away with their expander in the sidebar, the
list and the icon view, and stay folded until you open them again (a filter match
opens what it matched).

**Renaming without a dialog**: click a selected row's Connection, Host or User cell a
second time and type — the slow double-click every file manager uses. On a group
heading row the Connection cell renames the group, which moves the folder with
everything in it.

## The manager

The window has three layouts (the Layout button, saved as `layout =` in `ui.conf`):
**Browse** — the everyday view: a filter-first flat list with match highlighting, a
collapsible group sidebar (the ⊞ button), Live and per-protocol filter chips, and
live sessions appearing as a strip of one-line cards only while any exist — each with
👁 to focus it and an unplugged-cable button to disconnect. Browse also has an **icon
view** (the grid button, saved as `browse_view` in `ui.conf`): every connection as a
machine with its protocol chips across its screen — wrapping, spilling past the case
when there are many, the default underlined — grouped under the same headings and
tickboxes as the list, with a details pane beside the grid carrying the full record of
whatever is selected. A single click selects; double-click a chip to connect over that
protocol, or the tile for its default; **Inspector** — a
read-only audit showing where every connection's password comes from and whether its
protocols are healthy; **Classic** — the original window. (Earlier builds shipped
Browse's variants as separate Rail/Spotlight/Cockpit modes; they were one layout
differing in toggles, so now they are toggles — old `layout =` values migrate.)
Pinned filter tabs (Ctrl+T) sit under the headerbar and are shared by every layout. Configuration problems never block the window: they surface in the status
bar, on the Setup badge, and per-row.

The hamburger menu is gone — **Setup** (sidebar footer, or the Layout menu) carries
Protocols, Credentials, Keyboard shortcuts and Import/Export as cards over the same
plain files. With zero connections the window opens as a first-run checklist.

Pinned filter tabs belong to the connection list, so they appear with it and step
aside on Setup, Logs and the first-run checklist.

**F1 opens the Help window** anywhere: the guide is generated from the source
docstrings that implement each behaviour, and every topic ends with an Open-code
link to that file and line.

The **Net** column probes reachability (one TCP connect at a time, visible rows
first, paused while hidden — cheap even at hundreds of hosts); offline rows with a
learned MAC offer **⚡ Wake** (`rcm wake <sel>`). Right-click also gives **Open
terminal** (embedded VTE tabs when the library is present, each in its own scroller
so the prompt never scrolls out of reach — right-click the *tab* to pop that terminal
into its own window, or right-click inside it for **Send script** — it types a file from `scripts/` into the live session with the
placeholders substituted, `{password}` deliberately never), **Run script** across
the selection — SSH when port 22 answers, PowerShell over WinRM otherwise, live
per-host verdicts in the dialog, output captured per run — and **History**.
Everything notable — launches, scripts, probes, wakes, tunnels — is one JSONL line
under `logs/`; the **Logs page** (▤ in the sidebar, or the Layout menu) filters it
by kind, text and range back to 90 days, shows each event with its captured output,
and exports the filtered view as CSV. History is the same page pre-filtered to one
connection. **Tags** (`rcm-tags`, edited in the Edit dialog) are free labels
orthogonal to groups: they appear as #chips with counts under TAGS in the Browse
sidebar — click one to filter — and match in the filter box. **Workspaces**
(`workspaces.conf`, `rcm workspace <name>`) open named sets together, focusing
members that are already live; they sit under WORKSPACES in the same sidebar
(click shows the members, double-click connects them all), a member with a
monitor number gets its window moved there once it appears, and
`workspace:<name> = <combo>` under `[sessions]` in shortcuts.conf binds a key to
the whole set. Build them with right-click ▸ **Save as workspace…** on a
selection, then shape members, launch order (drag), monitors and the shortcut
under **Setup ▸ Workspaces**.

## Keyboard shortcuts

Set a connection's shortcut in its **Edit** dialog; the general keys live under
**Keyboard shortcuts…**. Defaults:

| Key | Action |
|---|---|
| `Super+R` | raise the manager |
| `Super+W` / `Super+Q` | switcher: hold Super, tap to move, release to switch |
| `Super+Alt+1…` | jump to a connection — focus if live, connect if not |

The switcher grid shows live sessions as solid cards and every shortcut-bound
connection that is *not* running as a dashed one. **W and Q cycle live sessions
only** — switching means switching — while a dashed card is a launcher you
**click**. Clicking any card commits it immediately; clicking the backdrop
cancels. With nothing running, nothing is selected, so releasing Super closes
the grid instead of connecting something you did not pick.

Escape and Enter work only when the switcher can take the keyboard, which it
cannot while Super is held: Cinnamon owns a keyboard grab for its own binding
for the whole hold, so the popup gets the pointer instead and says so in its
footer. That is also why W and Q keep working — each repeat re-fires the
Cinnamon binding, which signals the running switcher.

```bash
rcm shortcuts list|install|remove
```

They are stored as Cinnamon custom keybindings with ids prefixed `rcm-`; `remove`
touches only those.

## Command line

```
rcm init | list | sessions | gui | focus | next | prev | goto <sel>
rcm connect <sel> [--<protocol>] [--via <launcher>]
rcm connect-all <group>
rcm protocols | detect <protocol> [--write] | creds ... | shortcuts ...
rcm import <csv> [--overwrite] [--dry-run] | export [csv] | phonebook [out.rpb]
rcm calibrate <sel> [protocol] | gen-launcher | install-icon
```

## Notes from building this

Things that cost real time, kept here so they cost you less:

- **`xdotool type --window` does not work with Wine.** That flag uses `XSendEvent`,
  which Wine ignores outright — the dialog never fills and nothing errors. Always
  `xdotool windowactivate --sync` first, then type to the focused window via XTEST.
- **An Alt-Tab-style popup cannot grab the keyboard under Cinnamon.** While the
  modifier is held the WM already holds the grab, so `Gdk.Seat.grab` returns
  `GDK_GRAB_ALREADY_GRABBED` and the popup receives no keys at all. The repeat press
  reaches the WM, which launches the command again — so the second process signals the
  running popup (SIGUSR1/SIGUSR2) rather than exiting. Modifier release is detected by
  polling X, which needs no grab.
- **On X11 the Super key sets MOD4**, and GDK's virtual `SUPER_MASK` never appears in
  the raw mask from `get_device_position`. Watching only `SUPER_MASK` means you never
  see Super held.
- **Connection names may contain spaces.** Unquoted `.desktop` `Exec=` arguments split
  in two — and `desktop-file-validate` does *not* flag it — and a `(\S+\.rdp)` regex
  over `ps` output captures only `1.rdp`. Quote Exec arguments; match sessions by
  full-path substring.
- **An empty `Actions=;` in a `.desktop` file is a validation error**, so omit the key
  entirely when there is nothing to list.
- **In bash, `${var:-MASK}` prints the value when set.** It is a fallback, not a mask.
  Using it to redact a password prints the password.
- **`pkill -f <pattern>` matches its own shell's command line.** When cleaning up test
  processes that share a pattern with real sessions, discriminate on
  `readlink /proc/<pid>/exe` instead.
- Wayland breaks the Radmin injection silently; `ydotool` is the fallback.

### The Radmin phonebook format

`rcm phonebook` writes a `.rpb` Radmin Viewer can open, and never touches your real
one. The format was reverse-engineered: a 153-byte header (record count at `0x00`,
record stride at `0x0c`), then fixed-size 6138-byte records that are ~99% zero padding,
with UTF-16LE strings at fixed offsets — host `4904`, name `5104`, user `5908`. Folder
records instead pack a literal `folder` marker straight after the name's terminator, so
it moves with the name length. Verified by rebuilding an existing phonebook from its own
records byte-for-byte.

Known limits: folder *membership* is not reconstructed, so exported hosts may land at
the tree root; and since the format has no nested folders, a sub-group becomes a folder
named with its full path (`Plant/Line2`). Names, hosts and usernames are correct.

## Roadmap

- Windows support, in phases. **Done: the platform seam** — every OS call lives in
  `bin/rcm_platform.py` (`LinuxX11` is the reference behaviour; `WindowsNative`
  really implements detached spawn, process listing, ARP lookup, opening files and
  credentials via the `keyring` package, and raises a clear `PlatformError` for
  window control, monitor placement, credential typing and system shortcuts).
  Still to come: the tray agent owning `RegisterHotKey` and window management
  (Windows has no per-command keybinding registry, and Win+W/Win+Q are
  shell-reserved), then PyInstaller bundling GTK. Nothing of the Windows class has
  met a real Windows machine yet — its parsers are tested against fixtures.
- VNC is implemented but has not been exercised against a real server
- The WinRM script transport is exercised against a stub in tests, not yet against
  a real Windows host; same for via-gateways and a real bastion

## Icon

`assets/` holds the icon in two variants: the detailed one, and a simplified one for
16–32px where the screen shapes and their highlights turn to mush. `rcm install-icon`
puts the SVG in `hicolor/scalable/apps` and renders PNGs for each size, picking the
right variant per size. `rcm gen-launcher` installs it automatically if it is missing,
since the `.desktop` it writes references the icon by name.

## License

MIT — see [LICENSE](LICENSE).
