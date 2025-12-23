param(
    [string]$Python = "python",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating venv at .venv"
    & $Python -m venv .venv
}

if ($Clean) {
    Write-Host "Cleaning build artifacts"
    Remove-Item -ErrorAction SilentlyContinue -Recurse -Force `
        (Join-Path $root "build"), `
        (Join-Path $root "dist"), `
        (Join-Path $root "server.spec")
}

Write-Host "Installing build dependencies (pyinstaller) and runtime deps"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install --upgrade -r requirements.txt pyinstaller

Write-Host "Building SE-Radio-Server.exe"
& $venvPy -m PyInstaller --noconfirm --clean --onefile `
    --name "SE-Radio-Server" `
    --add-data "config.json;." `
    server.py

Write-Host "Build complete. Output: dist\SE-Radio-Server.exe"
