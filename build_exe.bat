@echo off
setlocal EnableExtensions
title Building RumbleCut.exe
cd /d "%~dp0"

rem  Usage:  build_exe.bat              -> folder build  dist\RumbleCut\RumbleCut.exe
rem          build_exe.bat onefile      -> single file   dist\RumbleCut.exe
rem          build_exe.bat whisper      -> also bundles speech recognition (bigger)
rem          build_exe.bat onefile whisper

echo.
echo  ===== RumbleCut build =====
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo  Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  [1/4] Creating a private Python environment...
  %PY% -m venv .venv || goto :fail
)
set "VPY=.venv\Scripts\python.exe"

echo  [2/4] Installing libraries (first run takes a few minutes)...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install --upgrade -r requirements.txt pyinstaller || goto :fail
echo %* | find /i "whisper" >nul && ("%VPY%" -m pip install --upgrade faster-whisper || goto :fail)

echo  [3/4] Getting FFmpeg...
if not exist "vendor\ffmpeg\ffmpeg.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\get_ffmpeg.ps1" || goto :fail
) else (
  echo        already present
)

echo  [4/4] Building the .exe...
set "RUMBLECUT_ONEFILE="
echo %* | find /i "onefile" >nul && set "RUMBLECUT_ONEFILE=1"
"%VPY%" -m PyInstaller --noconfirm --clean RumbleCut.spec || goto :fail

echo.
if defined RUMBLECUT_ONEFILE (
  echo  DONE:  dist\RumbleCut.exe
  start "" explorer "%CD%\dist"
) else (
  echo  DONE:  dist\RumbleCut\RumbleCut.exe   ^(keep the whole RumbleCut folder together^)
  start "" explorer "%CD%\dist\RumbleCut"
)
echo.
pause
exit /b 0

:fail
echo.
echo  BUILD FAILED - scroll up for the first error message.
pause
exit /b 1
