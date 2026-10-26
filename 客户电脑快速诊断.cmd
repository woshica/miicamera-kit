@echo off
chcp 65001 >nul
if not exist "%~dp0runtime\python\python.exe" (
  echo 缺少随包Python，请先完整复制miicamera-kit文件夹后运行。
  pause
  exit /b 1
)
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0scripts\diagnose_pc.py" %*
set "diagnostic_exit_code=%errorlevel%"
echo.
echo 诊断结束。0=当前配置短录通过，1=发现失败项，2=实际短录未验证或中止。请以报告为准。
pause
exit /b %diagnostic_exit_code%
