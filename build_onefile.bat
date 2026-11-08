@echo off
rem Builds a single self-contained dist\RumbleCut.exe (everything inside one file)
cd /d "%~dp0"
call build_exe.bat onefile
