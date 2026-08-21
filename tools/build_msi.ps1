# Build GRebelsTelemetry.msi
#
#   powershell -ExecutionPolicy Bypass -File tools\build_msi.ps1
#
# Fetches the WiX 3.14 binaries on first run (they are a plain zip, no install
# and no .NET SDK required, unlike WiX 4+ which is a dotnet tool). Builds both
# executables, then compiles the installer.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$wixDir = Join-Path $root "tools\wix"
$candle = Join-Path $wixDir "candle.exe"
$light  = Join-Path $wixDir "light.exe"

if (-not (Test-Path $candle)) {
    Write-Host "Fetching the WiX 3.14 toolset..."
    $zip = Join-Path $env:TEMP "wix314-binaries.zip"
    $url = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    New-Item -ItemType Directory -Force -Path $wixDir | Out-Null
    Expand-Archive -Path $zip -DestinationPath $wixDir -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $candle)) { throw "WiX extracted but candle.exe is missing" }
}

# --- executables ----------------------------------------------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
    if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
        uv venv --python 3.12 .venv-build
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
    }
    $python = Join-Path $root ".venv-build\Scripts\python.exe"
    uv pip install --python $python pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "installing pyinstaller failed" }
} else {
    $python = "python"
    & $python -m pip install --upgrade pip pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "installing pyinstaller failed" }
}

$version = (Select-String -Path "src\grebels_telemetry\__init__.py" `
    -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building version $version"

# The GUI app. Windowed: it must not flash a console.
& $python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name GRebelsTelemetry --paths src `
    --add-data "mod;mod" --add-data "ue4ss-5.8;ue4ss-5.8" --add-data "simhub;simhub" `
    --hidden-import grebels_telemetry.app run_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed on the app" }

# The setup helper. Console: the MSI runs it silently and captures its output,
# and a windowed build would give us nothing to put in the log.
& $python -m PyInstaller --noconfirm --clean --onefile --console `
    --name grsetup --paths src `
    --add-data "simhub;simhub" `
    grsetup.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed on the setup helper" }

# --- installer ------------------------------------------------------------
# MSI ProductVersion only honours three numeric fields, so a suffix like
# "0.3.0-beta" has to be trimmed or light.exe rejects it.
$msiVersion = ($version -replace '[^0-9.].*$', '')
$parts = $msiVersion.Split('.')
while ($parts.Count -lt 3) { $parts += "0" }
$msiVersion = ($parts[0..2] -join '.')
Write-Host "MSI ProductVersion $msiVersion"

$icon = Join-Path $root "simhub\icon.jpg"
if (-not (Test-Path $icon)) { throw "icon is missing: $icon" }
# .ico is required for ARPPRODUCTICON; convert the jpg we already ship.
Add-Type -AssemblyName System.Drawing
$icoPath = Join-Path $root "build\app.ico"
New-Item -ItemType Directory -Force -Path (Split-Path $icoPath) | Out-Null
$src = [System.Drawing.Image]::FromFile($icon)
$bmp = New-Object System.Drawing.Bitmap $src, 64, 64
$hIcon = $bmp.GetHicon()
$ico = [System.Drawing.Icon]::FromHandle($hIcon)
$fs = [IO.File]::Create($icoPath)
$ico.Save($fs); $fs.Close()
$bmp.Dispose(); $src.Dispose()

& $candle -nologo -arch x86 `
    -dProductVersion="$msiVersion" `
    -dAppExe="dist\GRebelsTelemetry.exe" `
    -dSetupExe="dist\grsetup.exe" `
    -dIconFile="$icoPath" `
    -ext WixUIExtension -ext WixUtilExtension `
    -out "build\Product.wixobj" "installer\Product.wxs"
if ($LASTEXITCODE -ne 0) { throw "candle failed" }

& $light -nologo `
    -ext WixUIExtension -ext WixUtilExtension `
    -cultures:en-us `
    -out "dist\GRebelsTelemetry-$version.msi" "build\Product.wixobj"
if ($LASTEXITCODE -ne 0) { throw "light failed" }

$msi = Join-Path $root "dist\GRebelsTelemetry-$version.msi"
$size = [math]::Round((Get-Item $msi).Length / 1MB, 1)
Write-Host ""
Write-Host "Built: $msi ($size MB)"
