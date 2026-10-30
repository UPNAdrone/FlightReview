#!/usr/bin/env bash
# Build a Linux one-folder app + Ubuntu .deb (double-click install via Software / dpkg).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="${APP_VERSION:-1.0.0}"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
DIST="$ROOT/dist"
APP_DIR="$DIST/UPNAdrone"
DEB_ROOT="$DIST/deb_root"
DEB_NAME="upnadrone_${VERSION}_${ARCH}.deb"

echo "==> Installing build deps into current Python env"
python3 -m pip install --upgrade pip pyinstaller
# Packaged builds always include Map support
python3 -m pip install -r requirements-map.txt

echo "==> Verifying map stack is importable"
python3 - <<'PY'
import contextily, geopandas, shapely, pyproj
print("OK:", contextily.__name__, geopandas.__name__, shapely.__name__, pyproj.__name__)
PY

echo "==> PyInstaller one-folder build (includes map libs)"
rm -rf build dist/UPNAdrone
pyinstaller --noconfirm packaging/upnadrone.spec

# Sanity-check that contextily landed in the bundle
if ! find "$APP_DIR" -type d -name 'contextily' 2>/dev/null | grep -q .; then
  echo "ERROR: contextily not found inside dist/UPNAdrone — map would be broken."
  exit 1
fi
echo "==> Map package present in bundle"

echo "==> Assembling .deb"
rm -rf "$DEB_ROOT"
install -d "$DEB_ROOT/DEBIAN"
install -d "$DEB_ROOT/opt/upnadrone"
install -d "$DEB_ROOT/usr/share/applications"
install -d "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps"
install -d "$DEB_ROOT/usr/bin"

cp -a "$APP_DIR/." "$DEB_ROOT/opt/upnadrone/"
if [[ -f "$ROOT/flight-review-logo.png" ]]; then
  cp "$ROOT/flight-review-logo.png" "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps/upnadrone.png"
fi

cat > "$DEB_ROOT/usr/share/applications/upnadrone.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=UPNAdrone Log Viewer
GenericName=PX4 Flight Log Analyzer
Comment=Analyze PX4 ULog flight logs
Exec=/opt/upnadrone/UPNAdrone
Icon=upnadrone
Categories=Science;Education;Utility;
Terminal=false
StartupNotify=true
EOF

# Convenience launcher on PATH
cat > "$DEB_ROOT/usr/bin/upnadrone" <<'EOF'
#!/bin/sh
exec /opt/upnadrone/UPNAdrone "$@"
EOF
chmod 755 "$DEB_ROOT/usr/bin/upnadrone"
chmod 755 "$DEB_ROOT/opt/upnadrone/UPNAdrone"

# Installed size in KiB
INSTALLED_SIZE="$(du -sk "$DEB_ROOT/opt" | awk '{print $1}')"

cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: upnadrone
Version: ${VERSION}
Section: science
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_SIZE}
Maintainer: UPNAdrone <upnadrone@local>
Depends: libtk8.6 | libtk8.5, libx11-6
Description: UPNAdrone Log Viewer
 Desktop tool to convert and review PX4 ULog flight logs
 with overview, plots, parameters, and GPS map views.
EOF

dpkg-deb --root-owner-group --build "$DEB_ROOT" "$DIST/$DEB_NAME"
echo
echo "Built:"
echo "  Folder app: $APP_DIR/UPNAdrone"
echo "  Installer:  $DIST/$DEB_NAME"
echo
echo "Install on Ubuntu:  sudo apt install ./$DEB_NAME"
echo "Or double-click the .deb in the file manager (Open with Software Install)."
