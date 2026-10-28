@echo off
chcp 65001 >nul
echo H.265 trial: 4K45, CQ23, 60 seconds, test segments of 20 nominal video seconds.
echo Stop ImageView preview/recording before starting. Requires NVIDIA HEVC encoder.
echo Files are saved outside this kit. Camera exposure will be set to 10ms, gain to 100%%.
pause
"%~dp0..\..\..\runtime\python\python.exe" -B -X utf8 "%~dp0..\..\..\scripts\capture.py" record --config "%~dp0..\..\config\mouse-4k45-hevc-trial.json" --duration 60 --segment-seconds 20
if errorlevel 1 echo Trial did not pass all checks. See capture.json and continuous.ffmpeg.log.
pause
