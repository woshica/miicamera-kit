@echo off
chcp 65001 >nul
"%~dp0..\..\runtime\python\python.exe" -B -X utf8 "%~dp0..\scripts\menu.py" --configured-check
set "recording_exit_code=%errorlevel%"
pause
exit /b %recording_exit_code%
