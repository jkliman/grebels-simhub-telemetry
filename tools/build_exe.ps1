# Build GRebelsTelemetry.exe
#
# PyInstaller does not always have a wheel for the very newest CPython, so this
# prefers uv to pin a Python it definitely supports rather than trusting
# whatever happens to be on PATH.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "Creating a build environment with uv (Python 3.12)..."
    uv venv --python 3.12 .venv-build
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
    $python = Join-Path $root ".venv-build\Scripts\python.exe"
    # A bare uv venv has no pip, so install through uv rather than python -m pip.
    uv pip install --python $python pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "installing pyinstaller failed" }
} else {
    Write-Host "uv not found, using the Python on PATH..."
    $python = "python"
    & $python -m pip install --upgrade pip pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "installing pyinstaller failed" }
}

$version = (Select-String -Path "src\grebels_telemetry\__init__.py" `
    -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building version $version"

& $python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name GRebelsTelemetry `
    --paths src `
    --add-data "mod;mod" `
    --add-data "ue4ss-5.8;ue4ss-5.8" `
    --hidden-import grebels_telemetry.app `
    run_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\GRebelsTelemetry.exe"
if (-not (Test-Path $exe)) { throw "build reported success but $exe is missing" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Built: dist\GRebelsTelemetry.exe ($size MB, version $version)"
