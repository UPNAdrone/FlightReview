# Packaging notes

| Script | Host | Output |
|--------|------|--------|
| `build_linux.sh` | Ubuntu / Debian | `dist/UPNAdrone/` + `dist/upnadrone_*.deb` |
| `build_windows.ps1` | Windows | `dist/UPNAdrone/` + portable zip + Setup.exe (with Inno) |
| `upnadrone.spec` | either | PyInstaller one-folder layout |

**Map support is mandatory for packaged builds.** Scripts install `requirements-map.txt` and the spec fails the build if `contextily` / `geopandas` are missing. PyInstaller uses `collect_all` so tile/CRS data ships inside the bundle.

CI: `/.github/workflows/build-installers.yml` builds both when you push a `v*` tag or run the workflow manually.
