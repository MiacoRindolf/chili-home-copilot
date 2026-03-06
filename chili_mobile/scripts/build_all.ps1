# One-command Windows build and installer for CHILI desktop app.
# Run from repo root: .\chili_mobile\scripts\build_all.ps1
# Requires: Flutter SDK, inno_build (dart run inno_build), Inno Setup (inno_build can install it).

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Join-Path $ScriptDir ".."
Push-Location $ProjectDir

try {
    Write-Host "Building Flutter Windows release..."
    flutter build windows --release
    if ($LASTEXITCODE -ne 0) { throw "flutter build failed" }

    Write-Host "Creating Windows installer (Inno Setup)..."
    dart run inno_build
    if ($LASTEXITCODE -ne 0) { throw "inno_build failed" }

    $InstallerDir = "build\windows\x64\installer"
    $SetupExe = Get-ChildItem -Path $InstallerDir -Filter "*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($SetupExe) {
        Write-Host ""
        Write-Host "Done. Installer: $($SetupExe.FullName)"
        Write-Host "Share this file with housemates for a one-click install."
    } else {
        Write-Host "Installer created under $InstallerDir (check for .exe)"
    }
} finally {
    Pop-Location
}
