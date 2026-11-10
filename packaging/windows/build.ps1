# Build dist\NetworkComputingHub\: NetworkComputingHub.exe (the manager,
# windowed), rcm.exe (the command line, console) and the runtime\ folder they
# share -- needing no Python, MSYS2 or GTK on the machine that runs them.
#
# The build itself needs MSYS2 with the packages in README (Windows), plus
#   pacman -S --needed mingw-w64-ucrt-x86_64-{pyinstaller,pyinstaller-hooks-contrib}
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 [-Msys C:\msys64] [-InstallTo <folder>]
#
# -InstallTo puts both .exe files and a fresh runtime\ into <folder>, the folder
# that holds (or will hold) RDP\ and the .conf files; nothing else there is
# touched.
param(
    [string]$Msys = "C:\msys64",
    [string]$InstallTo = ""
)
$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $Msys "ucrt64\bin\python.exe"
if (-not (Test-Path $python)) {
    throw "no MSYS2 UCRT64 Python at $python -- install MSYS2 or pass -Msys <folder>"
}
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing from $python -- in the MSYS2 UCRT64 shell: " +
          "pacman -S --needed mingw-w64-ucrt-x86_64-{pyinstaller,pyinstaller-hooks-contrib}"
}

# PyInstaller finds GTK's DLLs, typelibs and data through MSYS2's own tree.
$env:PATH = (Join-Path $Msys "ucrt64\bin") + ";" + $env:PATH

$work = Join-Path $root "build\windows"
$dist = Join-Path $root "dist"
$name = "NetworkComputingHub"
New-Item -ItemType Directory -Force $work | Out-Null
foreach ($old in @((Join-Path $dist $name), (Join-Path $dist "$name.exe"))) {
    if (Test-Path $old) { Remove-Item -Recurse -Force $old }
}

& $python (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $work "network-computing-hub.ico")
if ($LASTEXITCODE -ne 0) { throw "icon rendering failed (exit $LASTEXITCODE)" }

& $python -m PyInstaller --noconfirm --clean `
    --workpath $work --distpath $dist `
    (Join-Path $PSScriptRoot "rcm.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$out = Join-Path $dist $name
$size = (Get-ChildItem -Recurse -File $out | Measure-Object Length -Sum).Sum / 1MB
"{0}\{1}.exe ({2:N0} MB with runtime\)" -f $out, $name, $size

if ($InstallTo) {
    $target = (Resolve-Path $InstallTo).Path
    $running = Get-Process -Name $name, "rcm" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($target, "OrdinalIgnoreCase") }
    if ($running) { throw "$name is running from $target -- close it first" }
    # A stale runtime\ would mix two builds' DLLs, so it is replaced whole.
    $runtime = Join-Path $target "runtime"
    if (Test-Path $runtime) { Remove-Item -Recurse -Force $runtime }
    Copy-Item -Recurse (Join-Path $out "runtime") $runtime
    Copy-Item -Force (Join-Path $out "$name.exe"), (Join-Path $out "rcm.exe") $target
    "installed into $target"
}
