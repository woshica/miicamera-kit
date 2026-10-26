@echo off
chcp 65001 >nul
"%~dp0..\runtime\python\python.exe" -B -X utf8 "%~dp0..\scripts\verify-kit.py"
set "verify_exit_code=%errorlevel%"
pause
exit /b %verify_exit_code%
