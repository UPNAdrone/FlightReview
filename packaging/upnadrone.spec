# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for UPNAdrone Log Viewer (one-folder build).
# Packaged builds ALWAYS include Map support (contextily + geopandas).
# Build from log_viewer_app/:  pyinstaller packaging/upnadrone.spec

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

SPECDIR = Path(SPECPATH).resolve()
ROOT = SPECDIR.parent
ICON_PNG = ROOT / "flight-review-logo.png"

datas = []
binaries = []
hiddenimports = [
    "matplotlib.backends.backend_tkagg",
    "PIL._tkinter_finder",
    "pyulog",
    "pyulog.ulog2csv",
]

if ICON_PNG.exists():
    datas.append((str(ICON_PNG), "."))

# --- Map stack is required for frozen builds ---
_map_packages = (
    "contextily",
    "geopandas",
    "shapely",
    "pyproj",
    "xyzservices",
    "mercantile",
    "certifi",
    "pyogrio",
    "rasterio",
    "affine",
    "fiona",
    "geographiclib",
)

_missing = []
for _pkg in ("contextily", "geopandas", "shapely", "pyproj"):
    try:
        __import__(_pkg)
    except ImportError:
        _missing.append(_pkg)

if _missing:
    raise SystemExit(
        "ERROR: packaged builds require map dependencies.\n"
        "  Missing: " + ", ".join(_missing) + "\n"
        "  Install with:  pip install -r requirements-map.txt\n"
        "  Then re-run the packaging script."
    )

for _pkg in _map_packages:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as _exc:
        # Some transitive packages (fiona/rasterio) may be unused with pyogrio —
        # only hard-fail on the core set already verified above.
        print(f"NOTE: collect_all({_pkg!r}) skipped: {_exc}")

for _pkg in ("contextily", "geopandas", "pyproj", "xyzservices", "certifi"):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "log_viewer_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib.tests",
        "numpy.tests",
        "pandas.tests",
        "tkinter.test",
        "pytest",
        "IPython",
        "jupyter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UPNAdrone",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "flight-review-logo.ico") if (ROOT / "flight-review-logo.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="UPNAdrone",
)
