@echo off
REM ── One-click campaign run (no dashboard) ──
cd /d "%~dp0"
if not exist ".venv\" ( python -m venv .venv )
call .venv\Scripts\activate.bat
pip install -r requirements.txt >nul
python main.py
pause
