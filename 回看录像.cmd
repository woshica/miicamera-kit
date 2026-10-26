@echo off
chcp 65001 >nul
if "%~1"=="" (
    echo 请将需要回看的 MKV 或 MP4 视频文件拖到「回看录像.cmd」上。
    echo 播放窗口内按 Q 或 Esc 退出。
    pause
    exit /b 2
)
"%~dp0runtime\ffmpeg\bin\ffplay.exe" -autoexit -i "%~1"
set "playback_exit_code=%errorlevel%"
if not "%playback_exit_code%"=="0" pause
exit /b %playback_exit_code%
