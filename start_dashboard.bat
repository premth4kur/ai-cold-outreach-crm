@echo off
REM ── One-click launcher for the outreach dashboard (Windows) ──
cd /d "%~dp0"

if not exist ".venv\" (
  echo Creating virtual environment...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing/updating dependencies...
pip install -r requirements.txt >nul

echo.
echo Starting dashboard at http://127.0.0.1:5000
start "" "http://127.0.0.1:5000"
python -m dashboard.app

pause
