@echo off
REM Launch UPNAdrone Log Viewer from source (Windows).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment (.venv)...
  py -3 -m venv .venv
  if errorlevel 1 (
    python -m venv .venv
  )
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

python log_viewer_app.py %*
