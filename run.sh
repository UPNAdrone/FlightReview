#!/usr/bin/env bash
# Launch UPNAdrone Log Viewer from source (Linux / macOS).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment (.venv)…"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec python log_viewer_app.py "$@"
