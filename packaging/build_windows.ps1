# Build Windows one-folder app + double-click Setup.exe (Inno Setup).
# Run from any cwd:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Version = if ($env:APP_VERSION) { $env:APP_VERSION } else { "1.0.0" }

Write-Host "==> Installing build deps (Map support is required for packaged builds)"
python -m pip install --upgrade pip pyinstaller
python -m pip install -r requirements-map.txt
python -c "import contextily, geopandas, shapely, pyproj; print('OK map stack')"

# Optional .ico for the EXE / installer (from PNG)
if ((Test-Path "flight-review-logo.png") -and -not (Test-Path "flight-review-logo.ico")) {
    try {
        python -c @"
from PIL import Image
img = Image.open('flight-review-logo.png').convert('RGBA')
img.save('flight-review-logo.ico', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
print('Wrote flight-review-logo.ico')
"@
    } catch {
        Write-Warning "Could not create .ico from PNG (Pillow optional)"
    }
}

Write-Host "==> PyInstaller one-folder build (includes map libs)"
if (Test-Path "dist\UPNAdrone") { Remove-Item -Recurse -Force "dist\UPNAdrone" }
pyinstaller --noconfirm packaging\upnadrone.spec

$AppDir = Join-Path $Root "dist\UPNAdrone"
if (-not (Test-Path (Join-Path $AppDir "UPNAdrone.exe"))) {
    throw "PyInstaller did not produce dist\UPNAdrone\UPNAdrone.exe"
}
$ctx = Get-ChildItem -Path $AppDir -Recurse -Directory -Filter "contextily" -ErrorAction SilentlyContinue
if (-not $ctx) {
    throw "contextily not found inside dist\UPNAdrone — map would be broken. Rebuild after installing requirements-map.txt"
}
Write-Host "==> Map package present in bundle"

# Zip portable fallback (no installer needed)
$ZipPath = Join-Path $Root "dist\UPNAdrone-Log-Viewer-$Version-windows-portable.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath }
Compress-Archive -Path "$AppDir\*" -DestinationPath $ZipPath
Write-Host "Portable zip: $ZipPath"

# Inno Setup installer when available
$Iscc = @(
    "${env:LocalAppData}\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Iscc) {
    Write-Host "==> Building Setup.exe with Inno Setup"
    & $Iscc "/DMyAppVersion=$Version" "packaging\windows\installer.iss"
    Write-Host "Installer: dist\UPNAdrone-Log-Viewer-$Version-windows-setup.exe"
} else {
    Write-Warning "Inno Setup 6 not found — portable zip only."
    Write-Warning "Install from https://jrsoftware.org/isinfo.php then re-run for Setup.exe"
}

Write-Host "Done."
