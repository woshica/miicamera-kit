@echo off
chcp 65001 >nul
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0scripts\menu.py" --preview
