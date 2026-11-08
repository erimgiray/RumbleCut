@echo off
rem Run RumbleCut without building the .exe (handy while developing).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if not exist "vendor\ffmpeg\ffmpeg.exe" powershell -NoProfile -ExecutionPolicy Bypass -File "tools\get_ffmpeg.ps1"
".venv\Scripts\python.exe" app.py %*
