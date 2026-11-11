# PyInstaller spec for the Windows build: a windowed .exe carrying Python,
# GTK 3 and PyGObject from MSYS2's UCRT64 environment. Run it through
# build.ps1, which makes the icon first and uses MSYS2's own Python.
#
# One folder, not one file: a one-file build unpacks its 1,000-odd GTK files
# (80 MB) into %TEMP% on every start, and with antivirus scanning each of them
# that measured 40 s to the first line of `list`. Unpacked once here, the
# runtime sits in `runtime\` beside the .exe and starts in seconds.
#
# The .exe keeps no data inside: RDP/ and the .conf files live in the folder
# it sits in (see APP in bin/rcm). Only read-only resources are bundled.
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent
BIN = ROOT / "bin"
BUILD = ROOT / "build" / "windows"
NAME = "NetworkComputingHub"

a = Analysis(
    [str(BIN / "rcm")],
    pathex=[str(BIN)],
    # Imported inside functions, after rcm publishes itself as "rcm".
    hiddenimports=["rcm_gui", "rcm_switch", "rcm_help", "rcm_platform"],
    datas=[
        (str(ROOT / "assets" / "network-computing-hub.svg"), "assets"),
        (str(ROOT / "assets" / "network-computing-hub-small.svg"), "assets"),
        (str(ROOT / "assets" / "machine.svg"), "assets"),
        (str(ROOT / "templates" / "default.rdp"), "templates"),
    ],
    hooksconfig={
        "gi": {
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            "languages": ["en_US", "en_GB"],
            "module-versions": {"Gtk": "3.0", "Gdk": "3.0"},
        },
    },
    excludes=["tkinter", "unittest", "pydoc_data"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(BUILD / "network-computing-hub.ico"),
    console=False,          # a window, not a console, on double-click
    upx=False,              # UPX-packed DLLs are what antivirus flags first
    strip=False,
    debug=False,
    contents_directory="runtime",
)

# The same program as a console app, sharing runtime\: what `bin/rcm` is in a
# clone. A windowed .exe cannot print to the terminal it was started from,
# return an exit code PowerShell waits for, or prompt for a password.
cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rcm",
    icon=str(BUILD / "network-computing-hub.ico"),
    console=True,
    upx=False,
    strip=False,
    debug=False,
    contents_directory="runtime",
)

coll = COLLECT(
    exe,
    cli,
    a.binaries,
    a.datas,
    upx=False,
    strip=False,
    name=NAME,
)
