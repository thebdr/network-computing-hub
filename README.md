# Network Computing Hub

One place to keep your remote connections, launched with whatever client you already
use, with credentials in the OS keyring instead of a text file.

The command is `rcm`.

It is a **manager and launcher**, not a protocol implementation. RDP, VNC and Radmin
sessions are opened by external programs you configure — Thincast, `mstsc`, xfreerdp,
Remmina, TigerVNC, Radmin Viewer — so you keep the client you like and get grouping,
batch import, live session tracking and global shortcuts on top.

- **Connections in one tree**, grouped, defined by ordinary `.rdp` files
- **Credentials in the OS keyring** (libsecret / GNOME Keyring), never on disk, with a
  precedence chain from per-connection down to a global default, and per-protocol
  entries where a host needs different accounts for RDP and Radmin
- **Any client you like**, per protocol, configured as command lines with placeholders
- **Live session list** — see what is connected, focus it, disconnect it
- **System-wide shortcuts** and an Alt-Tab-style switcher across active sessions
- **Batch CSV import/export**, and an experimental Radmin phonebook export
- **Radmin credential injection**, because Radmin refuses to save passwords by design

Linux/X11 (developed on Cinnamon). A Windows port is planned — see [Roadmap](#roadmap).

---

## Install

Requirements: Python 3.11+, PyGObject (GTK 3), `xdotool`, `rofi` is *not* required.
Optional: `psutil`, and whichever clients you want to launch.

```bash
git clone <your-fork> ~/RemoteConnections
cd ~/RemoteConnections
./bin/rcm init          # writes launchers.conf, creds.conf, shortcuts.conf
ln -s ~/RemoteConnections/bin/rcm ~/bin/rcm
```

Open the manager once (`rcm gui`) and it will create any missing config for you.

## Quick start

```bash
rcm init                       # create config files
rcm radmin detect --write      # find Radmin, if you use it
rcm import hosts.csv           # or add connections in the GUI
rcm gui                        # the manager
```

CSV columns are `group,name,host,username,port`; only `name` and `host` are required.
Imported connections are generated from `templates/default.rdp`, so whatever you set
there is inherited by every host.

## How connections are stored

`RDP/<GROUP>/<Name>.rdp` — ordinary RDP files, which are also the single source of
truth for host and username. Two extra keys are ours; clients ignore keys they do not
recognise:

```
rcm-vnc-port:i:5900
rcm-protocols:s:rdp,vnc,radmin
```

Rename a group folder with a leading dot (`Plant` → `.Plant`) and it disappears from
every menu, list and export.

## Configuration

Everything is a plain file you can edit by hand; the GUI is a convenience over the
same files, never the only way in. **Nothing is guessed in code** — if a value is
missing or wrong, the app says which file and which key, instead of silently falling
back to something that happened to work on the author's machine.

| File | What |
|---|---|
| `launchers.conf` | which program connects each protocol, and the `[radmin]` block |
| `creds.conf` | scope → username. **No passwords** — those are in the keyring |
| `shortcuts.conf` | system-wide keys |

### Launchers

```ini
[rdp]
default = Thincast
Thincast = flatpak run com.thincast.client {file}
xfreerdp = xfreerdp3 /v:{host}:{port} /u:{user} +clipboard
mstsc    = mstsc {file}
```

Placeholders are substituted per argument, so paths with spaces stay one argument:
`{file} {host} {user} {port} {name}`. The toolbar buttons use `default`; right-click a
connection for the others. Edit in the GUI under **Launchers…**.

### Radmin

Radmin has no `/password:` switch — Famatech deliberately prevent saving passwords on
the Viewer machine — so the only route is typing into its dialog. Configure where it
lives, and it works whether installed, portable, or under Wine:

```ini
[radmin]
exe        = /path/to/Radmin.exe
wine       = wine-stable     ; leave EMPTY for a native install
wineprefix = ~/.local/share/wineprefixes/Radmin
port       = 4899
```

`rcm radmin detect` ranks the installs it can find; `--write` fills in the config.
`rcm calibrate <connection>` opens the auth dialog, reports exactly what it is, saves a
screenshot of it, and types nothing — use it if the dialog ever changes.

### Credentials

Passwords live in the login keyring. `creds.conf` holds only `key|username|`. Lookup is
most-specific-first, and within a scope a protocol-specific entry beats the generic one:

```
Group/Host@radmin → Group/Host → Group@radmin → Group → DEFAULT@radmin → DEFAULT
```

```bash
rcm creds list          # where each password lives, never the value
rcm creds set Group     # prompts without echo
rcm creds migrate       # move leftover plaintext into the keyring
```

## Keyboard shortcuts

Set a connection's shortcut in its **Edit** dialog; the general keys live under
**Keyboard shortcuts…**. Defaults:

| Key | Action |
|---|---|
| `Super+R` | raise the manager |
| `Super+W` / `Super+Q` | switcher: hold Super, tap to move, release to switch |
| `Super+Alt+1…` | jump to a connection — focus if live, connect if not |

```bash
rcm shortcuts list|install|remove
```

They are stored as Cinnamon custom keybindings with ids prefixed `rcm-`; `remove`
touches only those.

## Command line

```
rcm init | list | sessions | gui | focus | next | prev | goto <sel>
rcm connect <sel> [--rdp|--vnc|--radmin] [--via <launcher>]
rcm connect-all <group>
rcm launchers | radmin show|detect [--write] | creds ... | shortcuts ...
rcm import <csv> [--overwrite] [--dry-run] | export [csv] | phonebook [out.rpb]
rcm calibrate <sel> | gen-launcher
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

Known limit: folder *membership* is not reconstructed, so exported hosts may land at the
tree root. Names, hosts and usernames are correct.

## Roadmap

- Windows support: a platform seam replacing `xdotool`/`ps`/`gsettings`, `keyring` in
  place of libsecret, a tray agent owning `RegisterHotKey` (Windows has no per-command
  keybinding registry, and Win+W/Win+Q are shell-reserved), PyInstaller bundling GTK
- VNC is implemented but has not been exercised against a real server

## License

MIT — see [LICENSE](LICENSE).
