"""Bounded, camera-free encoder checks; no installed GPU-name inference.

The short synthetic throughput screen includes source/filter/startup cost. It
does not validate sustained camera capture, SDK delivery, USB, disk or queues.
"""
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

ENCODERS = (
    ("libx264", "Software H.264", True),
    ("libx265", "Software HEVC", True),
    ("h264_nvenc", "NVIDIA H.264", True),
    ("hevc_nvenc", "NVIDIA HEVC", True),
    ("h264_qsv", "Intel QSV H.264", False),
    ("hevc_qsv", "Intel QSV HEVC", False),
    ("h264_amf", "AMD AMF H.264", False),
    ("hevc_amf", "AMD AMF HEVC", False),
)
CAPABILITY_TIMEOUT = 8
BENCHMARK_TIMEOUT = 20
BENCHMARK_FRAMES = 45


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def _run(command, timeout, log_path, input_data=None):
    """Only kill our own child, including on Ctrl+C; retain partial progress."""
    started = time.perf_counter()
    process = None
    stdout = stderr = b""
    status, error, code = "completed", None, None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            status, error = "timeout", f"Exceeded {timeout} seconds"
        code = process.returncode
        if status == "completed" and code:
            status, error = "failed", f"Process exited {code}"
    except OSError as exc:
        status, error = "failed", str(exc)
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        raise
    finally:
        elapsed = time.perf_counter() - started
        log_path.write_text(json.dumps(command, ensure_ascii=False) + "\n\nSTDOUT\n" + _text(stdout)
                            + "\nSTDERR\n" + _text(stderr) + ("\nERROR\n" + error if error else ""),
                            encoding="utf-8", errors="replace")
    return {"status": status, "error": error, "returncode": code,
            "stdout": _text(stdout), "stderr": _text(stderr), "elapsed_s": elapsed}


def _settings(encoder, profile):
    supported = encoder in {name for name, _, recorder in ENCODERS if recorder}
    result = {"source": "recorder profile" if supported else "generic capability probe only",
              "pixel_format": "yuv420p"}
    if not supported:
        result.update(bitrate="2M", b_frames=0)
        return result
    result.update({key: profile.get(key, default) for key, default in (
        ("fps", 45), ("gop_frames", 90), ("segment_seconds", 300), ("b_frames", 0),
        ("crf", 18), ("nvenc_cq", 19), ("rate_control", "quality"),
        ("bitrate_mbps", 24), ("maxrate_mbps", 26), ("buffer_mbits", 52),
        ("nvenc_preset", "p4"), ("x265_preset", "superfast"), ("x265_params", {}))})
    result["x265_params"] = dict(result["x265_params"])
    if encoder.startswith("h264") or encoder == "libx264":
        result["b_frames"] = 0
    if encoder == "libx265" and result["rate_control"] != "quality":
        result["rate_control"] = "quality"
        result["note"] = "libx265 recorder supports quality only; this candidate uses profile CRF, not capped VBR"
    return result


def _encoder_args(encoder, settings):
    """Mirror Writer.start encoding options; output muxing intentionally differs."""
    s = settings
    args = ["-an", "-c:v", encoder]
    if s["source"] == "generic capability probe only":
        return args + ["-b:v", "2M", "-bf", "0", "-pix_fmt", "yuv420p"]
    rate = (["-b:v", f"{s['bitrate_mbps']:g}M", "-maxrate", f"{s['maxrate_mbps']:g}M",
             "-bufsize", f"{s['buffer_mbits']:g}M"] if s["rate_control"] == "capped_vbr" else None)
    if encoder == "libx264":
        args += ["-preset", "ultrafast"] + (rate or ["-crf", str(s["crf"])])
        args += ["-pix_fmt", "yuv420p", "-x264-params",
                 f"keyint={s['gop_frames']}:min-keyint={s['gop_frames']}:scenecut=0:open-gop=0:bframes=0"]
    elif encoder == "libx265":
        params = {"keyint": s["gop_frames"], "bframes": s["b_frames"], **s["x265_params"]}
        args += ["-preset", s["x265_preset"], "-crf", str(s["crf"]), "-pix_fmt", "yuv420p",
                 "-forced-idr", "1", "-x265-params", ":".join(f"{k}={v}" for k, v in params.items())]
    else:
        args += ["-preset", s["nvenc_preset"], "-rc", "vbr"]
        args += rate or ["-b:v", "0", "-cq", str(s["nvenc_cq"])]
        args += ["-pix_fmt", "yuv420p", "-g", str(s["gop_frames"]), "-bf", str(s["b_frames"]),
                 "-forced-idr", "1", "-no-scenecut", "1", "-rc-lookahead", "0"]
    segment_frames = s["fps"] * s["segment_seconds"]
    keys = (f"expr:eq(mod(n,{segment_frames}),0)" if encoder == "libx265" else
            f"expr:eq(mod(n,{s['gop_frames']}),0)+eq(mod(n,{segment_frames}),0)")
    return args + ["-force_key_frames", keys]


def _progress_frames(output):
    frames = [int(value) for value in re.findall(r"(?m)^frame=\s*(\d+)\s*$", output)]
    times = re.findall(r"(?m)^out_time=(.+)$", output)
    return max(frames, default=0), times[-1].strip() if times else None


def probe_encoders(ffmpeg, ffprobe, profile, report_dir, progress=None):
    """Return JSON-safe rows; write unique per-attempt logs and tiny MKV files.

    compiled is None if the FFmpeg listing itself failed. functional requires a
    real file with exactly three decoded frames according to ffprobe. A failed
    or timed-out throughput screen never reverses that capability result.
    """
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    listing_log = directory / f"encoders-{token}.log"
    listing = _run([str(ffmpeg), "-hide_banner", "-nostdin", "-encoders"], 8, listing_log)
    available = (set(re.findall(r"(?m)^\s*[VAS][A-Z.]{5}\s+(\S+)\s", listing["stdout"]))
                 if listing["status"] == "completed" else None)
    rows = []
    for encoder, label, supported in ENCODERS:
        if progress:
            progress(f"Checking {encoder}")
        stem = f"{encoder}-{token}"
        settings = _settings(encoder, profile)
        row = {"encoder": encoder, "label": label, "supported_by_recorder": supported,
               "compiled": encoder in available if available is not None else None,
               "functional": False, "status": "not_compiled", "error": None,
               "settings": settings, "log": listing_log.name,
               "capability": {"status": "skipped", "frames": None, "elapsed_s": None,
                              "video": None, "log": None, "probe_log": None},
               "benchmark": {"status": "skipped", "frames": 0, "requested_frames": BENCHMARK_FRAMES,
                             "elapsed_s": None, "fps": None, "target_fps": profile["fps"],
                             "meets_target": None, "error": None, "log": None,
                             "settings_note": settings.get("note"),
                             "scope": "Short synthetic 4K RGB24-to-yuv420p screen including startup/filter cost; not sustained camera capture validation"}}
        rows.append(row)
        if row["compiled"] is False:
            continue
        video, log = directory / f"{stem}.mkv", directory / f"{stem}.encode.log"
        args = _encoder_args(encoder, settings)
        command = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning", "-n",
                   "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "256x256",
                   "-framerate", str(profile["fps"]), "-i", "pipe:0", "-frames:v", "3"]
        command += args + ["-f", "matroska", str(video)]
        result = _run(command, CAPABILITY_TIMEOUT, log, bytes(256 * 256 * 3 * 3))
        row["capability"].update(status=result["status"], elapsed_s=round(result["elapsed_s"], 4),
                                 video=video.name, log=log.name)
        row["log"] = log.name
        row.update(status="capability_" + result["status"], error=result["error"])
        if result["status"] != "completed":
            if result["stderr"]:
                row["error"] = (row["error"] or "") + ": " + result["stderr"][-1200:].strip()
            continue
        probe_log = directory / f"{stem}.ffprobe.log"
        probe = _run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-count_frames",
                      "-show_entries", "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(video)],
                     CAPABILITY_TIMEOUT, probe_log)
        row["capability"]["probe_log"] = probe_log.name
        try:
            stream = json.loads(probe["stdout"])["streams"][0]
            frames = int(stream["nb_read_frames"])
            expected_codec = "hevc" if encoder.startswith("hevc") or encoder == "libx265" else "h264"
            valid = (probe["status"] == "completed" and frames == 3 and stream["codec_name"] == expected_codec
                     and stream["width"] == 256 and stream["height"] == 256 and not probe["stderr"].strip())
            row["capability"]["frames"] = frames
        except (ValueError, KeyError, IndexError, TypeError):
            valid = False
        if not valid:
            row.update(status="validation_failed", error=probe["error"] or "ffprobe did not confirm exactly 3 decodable 256x256 frames of expected codec")
            row["capability"]["status"] = "validation_failed"
            continue
        row.update(functional=True, status="functional", error=None)
        if not supported:
            continue
        if progress:
            progress(f"Screening {encoder} synthetic 4K throughput (up to {BENCHMARK_TIMEOUT}s)")
        bench_log = directory / f"{stem}.benchmark.log"
        command = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
                   "-progress", "pipe:1", "-nostats", "-f", "lavfi", "-i",
                   f"testsrc2=size=3840x2160:rate={profile['fps']}", "-vf", "format=rgb24,format=yuv420p",
                   "-frames:v", str(BENCHMARK_FRAMES)] + args + ["-f", "null", "-"]
        result = _run(command, BENCHMARK_TIMEOUT, bench_log)
        frames, out_time = _progress_frames(result["stdout"])
        fps = frames / result["elapsed_s"] if result["elapsed_s"] > 0 else None
        completed = result["status"] == "completed" and frames == BENCHMARK_FRAMES
        bench_status = result["status"] if result["status"] != "completed" or completed else "incomplete"
        row["benchmark"].update(status=bench_status, frames=frames, out_time=out_time,
                                elapsed_s=round(result["elapsed_s"], 4), fps=round(fps, 3) if fps is not None else None,
                                meets_target=(fps >= profile["fps"]) if completed and fps is not None else None,
                                error=result["error"] or (None if completed else "Did not report all requested frames"),
                                log=bench_log.name)
    return rows
