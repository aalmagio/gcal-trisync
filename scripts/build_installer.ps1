param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

& $Python -m PyInstaller gcal_trisync_gui.spec --noconfirm --clean

if (-not $IsccPath) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $IsccPath = $cmd.Source
    }
}

if (-not $IsccPath -or -not (Test-Path $IsccPath)) {
    throw "ISCC.exe non trovato. Installa Inno Setup o passa -IsccPath 'C:\Path\To\ISCC.exe'."
}

& $IsccPath installer\gcal-trisync.iss
