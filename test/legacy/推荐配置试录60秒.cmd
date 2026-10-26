@echo off
chcp 65001 >nul
"%~dp0..\..\runtime\python\python.exe" -B -X utf8 "%~dp0..\..\scripts\menu.py" --recommended-test
pause
