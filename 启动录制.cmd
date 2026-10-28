@echo off
chcp 65001 >nul
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0scripts\operator_console.py" %*
set "recording_exit_code=%errorlevel%"
if not "%recording_exit_code%"=="0" pause
exit /b %recording_exit_code%
