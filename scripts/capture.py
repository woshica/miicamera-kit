#!/usr/bin/env python3
"""Portable MiiCamera capture. Run with ../runtime/python/python.exe.

MKV uses constant frame rate (CFR): encoded frame i is displayed at i/fps, even
if acquisition was slower. Use frames.csv host and SDK timestamps for real time.
No frames are duplicated. A sequence gap or a measured rate outside +/-5% marks a run
incomplete and returns a nonzero exit status.
"""

import argparse
import ctypes
import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import zlib

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "scripts"))
from video_writer import Writer, resolve_encoder
from recording_profile import resolve_record_args
SDK_PY = KIT / "sdk" / "python"
SDK_DLL = KIT / "sdk" / "win" / "x64"
FFMPEG = KIT / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = KIT / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe"
MIN_FREE_BYTES = 10 * 1024**3
FATAL_EVENTS = (0x0010, 0x0011, 0x0080, 0x0081, 0x0082, 0x0085)



def sdk():
    if sys.platform != "win32":
        raise RuntimeError("This bundled SDK requires 64-bit Windows")
    if not SDK_PY.joinpath("miicam.py").is_file() or not SDK_DLL.joinpath("miicam.dll").is_file():
        raise RuntimeError("Missing bundled SDK Python wrapper or win/x64/miicam.dll")
    if str(SDK_PY) not in sys.path:
        sys.path.insert(0, str(SDK_PY))
    # Keep handle alive through the capture; required by Python 3.8+ DLL loading.
    if not hasattr(sdk, "dll_handle"):
        sdk.dll_handle = os.add_dll_directory(str(SDK_DLL))
    import miicam
    return miicam


def describe(dev, mii):
    flags = dev.model.flag
    if flags & mii.MIICAM_FLAG_USB30_OVER_USB20:
        usb = "USB 2.0 connection (USB 3 camera)"
    elif flags & mii.MIICAM_FLAG_USB32_OVER_USB30:
        usb = "USB 3.0 connection (USB 3.2 camera)"
    elif flags & mii.MIICAM_FLAG_USB32:
        usb = "USB 3.2 capable/connected"
    elif flags & mii.MIICAM_FLAG_USB30:
        usb = "USB 3.0 capable/connected"
    else:
        usb = "USB mode not reported"
    return {"name": dev.displayname, "id": dev.id, "model": dev.model.name,
            "usb": usb, "model_flags": hex(flags),
            "resolutions": [[r.width, r.height] for r in dev.model.res[:dev.model.preview]]}


def select_camera(mii):
    devices = mii.Miicam.EnumV2()
    if len(devices) != 1:
        raise RuntimeError(f"Expected exactly one camera; found {len(devices)}. Use probe to inspect connections")
    dev = devices[0]
    camera = mii.Miicam.Open(dev.id)
    if camera is None:
        raise RuntimeError("Camera found but could not be opened; close other camera software")
    return dev, camera


def configure(camera, dev, mii, exposure_us, gain, speed_level=2, sdk_fps_limit=0,
              frontend_buffer=16, backend_buffer=16, usb_priority=2):
    resolutions = [(r.width, r.height) for r in dev.model.res[:dev.model.preview]]
    try:
        index = resolutions.index((3840, 2160))
    except ValueError as exc:
        raise RuntimeError(f"3840x2160 not supported by {dev.model.name}: {resolutions}") from exc
    camera.put_eSize(index)
    camera.put_RealTime(0)
    camera.put_Option(mii.MIICAM_OPTION_FRONTEND_DEQUE_LENGTH, frontend_buffer)
    camera.put_Option(mii.MIICAM_OPTION_BACKEND_DEQUE_LENGTH, backend_buffer)
    camera.put_Option(mii.MIICAM_OPTION_THREAD_PRIORITY, usb_priority)
    camera.put_Option(mii.MIICAM_OPTION_RAW, 0)
    if not 0 <= speed_level <= dev.model.maxspeed:
        raise RuntimeError(f"Speed level {speed_level} outside 0..{dev.model.maxspeed}")
    # On this model, speed 2 supplies native 4K ~30 fps. Applying the SDK's
    # software 30 fps cap discards otherwise consecutive source frames.
    try:
        camera.put_Speed(speed_level)
    except mii.HRESULTException as exc:
        raise RuntimeError(f'Camera rejected speed level {speed_level}; close ImageView preview/recording and other camera users, then retry. SDK: {exc}') from exc
    camera.put_Option(mii.MIICAM_OPTION_FRAMERATE, sdk_fps_limit)
    camera.put_Option(mii.MIICAM_OPTION_UPSIDE_DOWN, 0)
    camera.put_VFlip(0)
    camera.put_HFlip(0)
    camera.put_Option(mii.MIICAM_OPTION_RGB, 0)  # RGB24
    camera.put_Option(mii.MIICAM_OPTION_BYTEORDER, 0)  # RGB, also on Windows
    camera.put_AutoExpoEnable(0)
    camera.put_ExpoTime(exposure_us)
    camera.put_ExpoAGain(gain)
    actual_exposure = camera.get_ExpoTime()
    actual_gain = camera.get_ExpoAGain()
    if actual_exposure != exposure_us or actual_gain != gain:
        raise RuntimeError(f"Camera did not accept manual exposure/gain: requested {exposure_us} us/{gain}%, got {actual_exposure} us/{actual_gain}%")
    width, height = camera.get_Resolution(index)
    if (width, height) != (3840, 2160):
        raise RuntimeError(f"Unexpected configured resolution {width}x{height}")
    settings = {"realtime_drop_mode": camera.get_RealTime(),
                "frontend_queue_frames": camera.get_Option(mii.MIICAM_OPTION_FRONTEND_DEQUE_LENGTH),
                "backend_queue_frames": camera.get_Option(mii.MIICAM_OPTION_BACKEND_DEQUE_LENGTH),
                "usb_thread_priority": camera.get_Option(mii.MIICAM_OPTION_THREAD_PRIORITY),
                "speed_level": camera.get_Speed(), "max_speed_level": dev.model.maxspeed,
                "auto_exposure": camera.get_AutoExpoEnable(),
                "exposure_us": actual_exposure, "gain": actual_gain,
                "raw": camera.get_Option(mii.MIICAM_OPTION_RAW),
                "rgb_mode": camera.get_Option(mii.MIICAM_OPTION_RGB),
                "byte_order": camera.get_Option(mii.MIICAM_OPTION_BYTEORDER),
                "frame_rate_limit": camera.get_Option(mii.MIICAM_OPTION_FRAMERATE),
                "upside_down": camera.get_Option(mii.MIICAM_OPTION_UPSIDE_DOWN),
                "vertical_flip": camera.get_VFlip(), "horizontal_flip": camera.get_HFlip(),
                "white_balance_temp_tint": camera.get_TempTint()}
    try:
        settings["white_balance_rgb_gain"] = camera.get_WhiteBalanceGain()
    except mii.HRESULTException as exc:
        if exc.hr & 0xffffffff != 0x80004001:  # E_NOTIMPL: temp/tint cameras
            raise
        settings["white_balance_rgb_gain"] = None
        settings["white_balance_rgb_gain_note"] = "Not implemented by this camera; temp/tint reported instead"
    if (any(settings[k] for k in ("realtime_drop_mode", "auto_exposure", "raw", "rgb_mode", "byte_order",
                                  "upside_down", "vertical_flip", "horizontal_flip"))
            or settings["frame_rate_limit"] != sdk_fps_limit
            or settings["speed_level"] != speed_level
            or settings["frontend_queue_frames"] != frontend_buffer
            or settings["backend_queue_frames"] != backend_buffer
            or settings["usb_thread_priority"] != usb_priority):
        raise RuntimeError(f"Camera settings readback mismatch: {settings}")
    return width, height, settings


def make_png(rgb, width, height):
    """Small dependency-free RGB PNG encoder for a single snapshot."""
    def chunk(kind, payload):
        body = kind + payload
        return len(payload).to_bytes(4, "big") + body + (zlib.crc32(body) & 0xffffffff).to_bytes(4, "big")
    stride = width * 3
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)
        scanlines.extend(rgb[y * stride:(y + 1) * stride])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", width.to_bytes(4, "big") +
            height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00") +
            chunk(b"IDAT", zlib.compress(scanlines, 1)) + chunk(b"IEND", b""))


def frame_meta(info, host_utc_ns, host_mono_ns):
    v = info.v3
    return {"sdk_seq": v.seq if v.flag & 1 else None,
            "sdk_timestamp_us": v.timestamp if v.flag & 2 else None,
            "host_utc_ns": host_utc_ns, "host_monotonic_ns": host_mono_ns,
            "sdk_flag": v.flag, "exposure_us": v.expotime if v.flag & 4 else None,
            "gain": v.expogain if v.flag & 8 else None}



def require_disk(path, required_bytes=MIN_FREE_BYTES):
    free = shutil.disk_usage(path).free
    if free < required_bytes:
        raise RuntimeError(f"保存盘空间不足：可用 {free / 1e9:.1f} GB，本次计划需至少 {required_bytes / 1e9:.1f} GB（包含预留空间）；请选择更大的保存盘或缩短时长。")


def require_recording_budget(run_dir, max_gb):
    if max_gb is None:
        return
    used = sum(path.stat().st_size for path in Path(run_dir).iterdir() if path.is_file())
    if used >= max_gb * 1e9:
        raise RuntimeError(f'Recording size protection reached {max_gb:g} GB; stopping as incomplete, without deleting or overwriting existing data')


def validate_video_counts(run_dir, segments, progress=None):
    for index, seg in enumerate(segments):
        if progress:
            progress(index, seg['video'])
        result = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                                 "-count_frames", "-show_entries", "stream=nb_read_frames",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(run_dir / seg["video"])],
                                capture_output=True, text=True,
                                timeout=max(120, (seg['last_host_monotonic_ns'] - seg['first_host_monotonic_ns']) / 1e9 * 2 + 30))
        if result.returncode:
            raise RuntimeError(f"ffprobe failed for {seg['video']}: {result.stderr.strip()}")
        try:
            count = int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(f"ffprobe did not report a frame count for {seg['video']}") from exc
        seg["frames_decoded"] = count
        if count != seg["frames_submitted"]:
            raise RuntimeError(f"Frame count mismatch in {seg['video']}: submitted {seg['frames_submitted']}, decoded {count}")
        if progress:
            progress(index + 1, None)


def run_capture(args):
    monitor = None
    try:
        if getattr(args, 'control_dir', None):
            from operator_camera import CameraMonitor
            monitor = CameraMonitor(args.control_dir)
            monitor.publish('starting', frames=0)
        return _run_capture(args, monitor)
    except Exception as exc:
        if monitor is not None:
            monitor.publish('incomplete', frames=0, error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if monitor is not None:
            monitor.finish()


def _run_capture(args, monitor=None):
    requested_encoder = getattr(args, 'encoder', None)
    if args.command == 'record':
        if not FFMPEG.is_file() or not FFPROBE.is_file():
            raise RuntimeError('Bundled ffmpeg.exe or ffprobe.exe missing')
        base = args.output_dir.resolve()
        base.mkdir(parents=True, exist_ok=True)
        required = args.storage_plan['required_free_bytes'] if args.storage_plan else MIN_FREE_BYTES
        require_disk(base, required)
        if args.storage_plan:
            print(f"Storage plan: target video {args.storage_plan['target_video_bytes']/1e9:.1f} GB; free space required with margin {required/1e9:.1f} GB", flush=True)
        args.encoder = resolve_encoder(requested_encoder, FFMPEG)
        print(f'Encoder: requested={requested_encoder}, selected={args.encoder}', flush=True)
    mii = sdk()
    dev, camera = select_camera(mii)
    fatal = []
    def on_event(event, _):
        if event in FATAL_EVENTS:
            fatal.append(event)
    started = False
    run_dir = None
    writer = None
    live_preview = None
    preview_active = False
    preview_fault = None
    status_display = None
    status_fault = None
    status_queue_capacity = None
    start_ns = None
    capture_elapsed_s = None
    frames = gaps = validated_segments = 0
    first_frame_ns = last_frame_ns = None
    awake = False
    result = {"status": "incomplete", "error": None, "stopped_by_user": False}

    def announce(message, error=False):
        # The status worker owns the live console block; permanent messages
        # must go through it so the next refresh cannot erase them.
        if status_display is not None and not status_fault:
            status_display.notice(message)
        else:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)

    def publish_status(stage, validating_segment=None):
        nonlocal status_fault
        try:
            elapsed_s = capture_elapsed_s
            if elapsed_s is None:
                elapsed_s = (time.monotonic_ns() - start_ns) / 1e9 if start_ns is not None else 0
            cadence = ((frames - 1) * 1e9 / (last_frame_ns - first_frame_ns)
                       if frames > 1 and last_frame_ns > first_frame_ns else None)
            jobs = getattr(writer, 'jobs', None)
            if monitor is not None:
                monitor.publish(stage, elapsed_s=elapsed_s, frames=frames,
                                measured_fps=cadence, sequence_gaps=gaps,
                                queue_depth=jobs.qsize() if jobs is not None else 0,
                                queue_capacity=status_queue_capacity,
                                run_dir=str(run_dir) if run_dir else None,
                                error=result['error'], readback=result.get('camera_settings_readback'),
                                stopped_by_user=result['stopped_by_user'])
            if status_display is None or status_fault:
                return
            status_display.publish(stage=stage, elapsed_s=elapsed_s, frames=frames,
                                   measured_fps=cadence, sequence_gaps=gaps,
                                   queue_depth=jobs.qsize() if jobs is not None else 0,
                                   queue_capacity=status_queue_capacity,
                                   validated_segments=validated_segments,
                                   validating_segment=validating_segment, error=result['error'])
        except Exception as exc:
            # Progress display is optional; never stop acquisition for display errors.
            status_fault = str(exc)

    def validation_progress(completed, current):
        nonlocal validated_segments
        validated_segments = completed
        publish_status('validating', current)

    def finish_preview():
        nonlocal live_preview, preview_active
        if live_preview is not None:
            try:
                live_preview.finish()
                result['preview'] = live_preview.snapshot()
                if preview_fault:
                    result['preview']['integration_error'] = preview_fault
            except Exception as exc:
                result['preview'] = {'enabled': True, 'status': 'failed', 'error': str(exc)}
                announce(f'预览收尾异常（录像仍会继续收尾）：{exc}')
            finally:
                live_preview = None
                preview_active = False
    try:
        if args.command == "record" and dev.model.flag & mii.MIICAM_FLAG_USB30_OVER_USB20 and args.fps >= 30:
            raise RuntimeError("Camera is on a USB 2.0 connection; reconnect to a USB 3 port before 4K/30 recording")
        width, height, settings = configure(camera, dev, mii, args.exposure_us, args.gain,
                                            args.speed_level, args.sdk_fps_limit,
                                            args.frontend_buffer, args.backend_buffer, args.usb_priority)
        if getattr(args, 'imaging', None) is not None:
            from camera_profile import apply_imaging
            settings['imaging'] = apply_imaging(camera, mii, args.imaging)
            # Base configure read this before the profile's fixed white balance was applied.
            settings['white_balance_temp_tint'] = (
                settings['imaging']['white_balance_temp'], settings['imaging']['white_balance_tint'])
            final_size = camera.get_FinalSize()
            if final_size != (width, height):
                raise RuntimeError(f'Final image size after imaging settings is {final_size}, expected {(width, height)}')
            settings['final_image_size'] = list(final_size)
        if monitor is not None:
            monitor.width, monitor.height = width, height
        result.update({"camera": describe(dev, mii), "width": width, "height": height,
                       "requested_fps": args.fps, "camera_settings_readback": settings, "exposure_us": args.exposure_us,
                       "gain": args.gain, "encoding_timing": "CFR; frame n displayed at n/requested_fps; frames.csv holds acquisition times"})
        if args.command == "record":
            require_disk(base)
            run_dir = base / ("capture-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            run_dir.mkdir()
            writer = Writer(run_dir, width, height, args.fps, args.segment_seconds, args.encoder,
                            gop_frames=args.gop_frames, b_frames=args.b_frames,
                            crf=args.crf, nvenc_cq=args.nvenc_cq,
                            rate_control=args.rate_control, bitrate_mbps=args.bitrate_mbps,
                            maxrate_mbps=args.maxrate_mbps, buffer_mbits=args.buffer_mbits,
                            nvenc_preset=args.nvenc_preset,
                            x265_preset=args.x265_preset, x265_params=args.x265_params)
            writer.start()
            status_queue_capacity = getattr(writer, 'queue_frames', '?')
            result['preview'] = {'enabled': args.preview_enabled, 'status': 'disabled'}
            result['status_display'] = {'enabled': args.status_enabled,
                                        'interval_seconds': args.status_interval_seconds,
                                        'write_timing_window_seconds': 1,
                                        'write_timing_basis': 'Mean duration of successful RGB writes to encoder stdin completed in the past 1 second; excludes queue wait and CSV; not GPU encode or disk flush time'}
            if args.preview_enabled:
                try:
                    from live_preview import LivePreview
                    live_preview = LivePreview(width, height, fps=args.preview_fps,
                                               max_width=args.preview_max_width)
                    preview_active = live_preview.start()
                    result['preview'] = live_preview.snapshot()
                    if preview_active:
                        print(f'录制预览：最多{args.preview_fps}fps，最大宽度{args.preview_max_width}像素；关闭预览窗口不停止录像。', flush=True)
                    else:
                        print(f"预览未能启动，录像继续：{result['preview'].get('error')}", flush=True)
                except Exception as exc:
                    preview_fault = str(exc)
                    preview_active = False
                    result['preview'] = {'enabled': True, 'status': 'failed', 'error': preview_fault}
                    print(f'预览未能启动，录像继续：{exc}', flush=True)
            if args.keep_awake and sys.platform == 'win32':
                awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
                if not awake:
                    raise RuntimeError('Could not prevent automatic sleep during recording')
            result.update({"started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                           "sdk_version": mii.Miicam.Version(),
                           "encoder": args.encoder, "requested_duration_s": args.duration,
                           "segment_seconds": args.segment_seconds,
                           "requested_encoder": requested_encoder,
                           "encoding_settings": writer.encoding_settings,
                           "profile_source": args.profile_source,
                           "resolved_profile": args.resolved_profile,
                           "storage_plan": args.storage_plan,
                           "automatic_sleep_inhibited": awake,
                           "segment_boundary_basis": "Encoded frame count (nominal CFR video time); actual acquisition times are in CSV",
                           "segments": writer.segments})
            (run_dir / 'profile-used.json').write_text(
                json.dumps({'source': args.profile_source, 'resolved': args.resolved_profile,
                            'actual_encoder': args.encoder, 'encoding_settings': writer.encoding_settings,
                            'storage_plan': args.storage_plan,
                            'camera_settings_readback': settings}, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
            (run_dir / "capture.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists() or output.with_suffix(output.suffix + '.json').exists():
                raise RuntimeError(f"Snapshot output exists: {output}")
        camera.StartPullModeWithCallback(on_event, None)
        started = True
        start_ns = time.monotonic_ns()
        last_disk_ns = start_ns
        buffer = ctypes.create_string_buffer(width * height * 3)
        frames = 0
        gaps = 0
        last_seq = None
        first_frame_ns = None
        last_frame_ns = None
        last_status_ns = start_ns
        if args.command == 'record' and args.status_enabled:
            try:
                from recording_status import RecordingStatus
                status_display = RecordingStatus(run_dir, args.duration, args.fps,
                                                 args.segment_seconds, args.status_interval_seconds,
                                                 write_timing_provider=writer.write_timing_snapshot)
                publish_status('recording')
                status_display.start()
            except Exception as exc:
                status_fault = str(exc)
        while True:
            now_ns = time.monotonic_ns()
            if monitor is not None and monitor.stop_requested():
                result['stopped_by_user'] = True
                break
            if args.command == "record" and now_ns - start_ns >= args.duration * 1e9:
                break
            if fatal:
                raise RuntimeError(f"Camera fatal event: 0x{fatal[0]:04x}")
            if writer and writer.error:
                raise RuntimeError(f"Video writer failed: {writer.error}")
            if writer and now_ns - last_disk_ns > 2_000_000_000:
                require_disk(run_dir)
                require_recording_budget(run_dir, args.max_recording_gb)
                last_disk_ns = now_ns
            info = mii.MiicamFrameInfoV4()
            try:
                camera.WaitImageV4(2000, buffer, 0, 24, -1, info)
            except mii.HRESULTException as exc:
                if getattr(exc, "hr", 0) & 0xffffffff == mii.E_TIMEOUT:
                    raise RuntimeError("No camera frame for 2 seconds; camera stalled or disconnected") from exc
                raise
            host_mono_ns = time.monotonic_ns()
            host_utc_ns = time.time_ns()
            v = info.v3
            if (v.width, v.height) != (width, height):
                raise RuntimeError(f"Frame resolution changed to {v.width}x{v.height}")
            if v.flag & 1:
                if last_seq is not None:
                    delta = (v.seq - last_seq) & 0xffffffff
                    if delta == 0 or delta > 1_000_000:
                        raise RuntimeError(f"SDK sequence reset/repeated: {last_seq} -> {v.seq}")
                    gaps += delta - 1
                last_seq = v.seq
            meta = frame_meta(info, host_utc_ns, host_mono_ns)
            if args.command == "snapshot":
                png = make_png(buffer.raw[:width * height * 3], width, height)
                with output.open("xb") as out:
                    out.write(png)
                snapshot_info = {"output": str(output), "frame": meta, "camera": describe(dev, mii),
                                 "camera_settings_readback": settings}
                with output.with_suffix(output.suffix + '.json').open('x', encoding='utf-8') as sidecar:
                    json.dump(snapshot_info, sidecar, ensure_ascii=False, indent=2)
                print(json.dumps(snapshot_info, ensure_ascii=False, indent=2))
                return 0
            # Both consumers share the immutable copy; only the writer must keep every frame.
            rgb = buffer.raw[:width * height * 3]
            writer.submit(rgb, meta)
            if monitor is not None:
                monitor.offer(rgb)
            if preview_active:
                try:
                    live_preview.offer(rgb)
                except Exception as exc:
                    preview_fault = str(exc)
                    preview_active = False
                    announce(f'预览已停用，录像继续：{exc}')
            frames += 1
            first_frame_ns = first_frame_ns or host_mono_ns
            last_frame_ns = host_mono_ns
            if host_mono_ns - last_status_ns >= 250_000_000:
                publish_status('recording')
                last_status_ns = host_mono_ns
        if frames == 0:
            raise RuntimeError("No frames captured")
        capture_end_ns = time.monotonic_ns()
        capture_elapsed_s = (capture_end_ns - start_ns) / 1e9
        camera.Stop()
        started = False
        publish_status('finishing')
        announce('采集结束，正在收尾并逐段核验视频；请等待结果后再关闭CMD窗口。')
        finish_preview()
        writer.finish()
        writer = None
        require_recording_budget(run_dir, args.max_recording_gb)
        publish_status('validating')
        validate_video_counts(run_dir, result["segments"], validation_progress)
        elapsed = (last_frame_ns - first_frame_ns) / 1e9 if frames > 1 else 0
        measured_fps = (frames - 1) / elapsed if elapsed > 0 else 0
        result.update({"frames_captured": frames, "sdk_sequence_gaps": gaps,
                       "sequence_gap_note": "Gaps may include SDK rate limiting; source frame completeness is not verified when gaps exist.",
                       "sdk_reported_software_drops": camera.get_Option(mii.MIICAM_OPTION_NUMBER_DROP_FRAME),
                       "sdk_frontend_full_count": camera.get_Option(mii.MIICAM_OPTION_FRONTEND_FULL),
                       "sdk_backend_full_count": camera.get_Option(mii.MIICAM_OPTION_BACKEND_FULL),
                       "capture_elapsed_s": (capture_end_ns - start_ns) / 1e9,
                       "first_to_last_frame_s": elapsed, "measured_acquisition_fps": measured_fps,
                       "timing_note": "MKV plays at requested CFR; use CSV timestamps for elapsed-time analysis. No duplicates inserted."})
        if fatal:
            raise RuntimeError(f"Camera fatal event: 0x{fatal[0]:04x}")
        if gaps or not args.fps * 0.95 <= measured_fps <= args.fps * 1.05:
            raise RuntimeError(f"Acquisition incomplete: sequence gaps={gaps}, measured {measured_fps:.2f} fps vs requested {args.fps}")
        result["status"] = "complete"
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        announce(result["error"], error=True)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        was_success = result['status'] == 'complete'
        if capture_elapsed_s is None and start_ns is not None:
            capture_elapsed_s = (time.monotonic_ns() - start_ns) / 1e9
        if result['error']:
            publish_status('incomplete')
        if awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        if started:
            try:
                camera.Stop()
            except Exception as exc:
                announce(f"Camera stop failed: {exc}", error=True)
        finish_preview()
        try:
            camera.Close()
        except Exception as exc:
            result["error"] = result["error"] or f"Camera close: {exc}"
        if writer:
            try:
                writer.finish()
            except Exception as exc:
                result["error"] = result["error"] or f"Writer shutdown: {exc}"
        if monitor is not None:
            if result['error']:
                result['status'] = 'incomplete'
            publish_status(result['status'])
            monitor.finish()
        if run_dir:
            if result['error']:
                result['status'] = 'incomplete'
            publish_status(result['status'])
            if status_display is not None:
                try:
                    status_display.finish()
                    result['status_display'].update(status_display.snapshot())
                except Exception as exc:
                    status_fault = status_fault or str(exc)
            if status_fault:
                result.setdefault('status_display', {})['error'] = status_fault
            result["segments"] = list(result.get("segments", []))
            (run_dir / "capture.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Run metadata: {run_dir / 'capture.json'}")
        if was_success and result['error']:
            return 1


def positive_float(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 604800:
        raise argparse.ArgumentTypeError("must be > 0 and <= 604800 (7 days)")
    return value


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def nonnegative_int(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="List SDK cameras, resolutions, and USB mode")
    snap = sub.add_parser("snapshot", help="Save one full-resolution RGB PNG without overwriting")
    snap.add_argument("--output", required=True, type=Path)
    rec = sub.add_parser("record", help="Capture segmented MKV plus per-frame CSV and capture.json")
    rec.add_argument('--control-dir', type=Path, help='Local operator status/preview and graceful stop directory')
    rec.add_argument('--config', type=Path, help='JSON or TOML recording profile; explicit command-line values override it')
    rec.add_argument('--dry-run', action='store_true', help='Validate and print resolved profile without opening camera')
    rec.add_argument("--output-dir", type=Path)
    rec.add_argument("--duration", type=positive_float, help="wall-clock seconds from stream start")
    rec.add_argument("--segment-seconds", type=positive_int)
    rec.add_argument("--fps", type=positive_int, help="requested CFR, compared with measured capture rate")
    rec.add_argument('--gop-frames', type=positive_int, help='Keyframe interval independent of file segmentation')
    rec.add_argument('--b-frames', type=nonnegative_int, help='HEVC encoders support 0..3; H264 requires 0')
    rec.add_argument('--crf', type=nonnegative_int, help='libx264/libx265 quality, 0..51')
    rec.add_argument('--nvenc-cq', type=positive_int, help='NVENC quality, 1..51')
    rec.add_argument('--nvenc-preset', choices=tuple(f'p{i}' for i in range(1, 8)))
    rec.add_argument('--x265-preset', choices=('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow', 'placebo'))
    rec.add_argument('--preview', dest='preview_enabled', action=argparse.BooleanOptionalAction,
                     default=None, help='Optional live preview; closing preview does not stop recording')
    rec.add_argument('--preview-fps', type=int, help='Preview refresh cap, 1..10; does not change recording fps')
    rec.add_argument('--preview-max-width', type=int, help='Preview maximum width, 320..1280; does not change recording size')
    rec.add_argument('--status', dest='status_enabled', action=argparse.BooleanOptionalAction,
                     default=None, help='Periodic recording progress in CMD')
    rec.add_argument('--status-interval-seconds', type=int, help='CMD progress interval, 1..60 seconds')
    rec.add_argument('--rate-control', choices=('quality', 'capped_vbr'))
    rec.add_argument('--bitrate-mbps', type=float, help='Target bitrate in capped_vbr mode')
    rec.add_argument('--maxrate-mbps', type=float, help='VBV bitrate ceiling in capped_vbr mode')
    rec.add_argument('--buffer-mbits', type=float, help='VBV buffer size in megabits')
    rec.add_argument('--max-recording-gb', type=float, help='Decimal GB stop threshold; reaching it marks capture incomplete')
    for command in (snap, rec):
        command.add_argument("--exposure-us", type=positive_int, default=10000 if command is snap else None)
        command.add_argument("--gain", type=positive_int, default=100 if command is snap else None, help="SDK gain percent")
        command.add_argument("--speed-level", type=nonnegative_int, default=2 if command is snap else None, help="camera speed level; 2 verified at 4K ~30 fps on this model")
        command.add_argument("--sdk-fps-limit", type=nonnegative_int, default=0 if command is snap else None, help="software frame-rate cap; 0 avoids SDK rate-limit frame drops")
        command.add_argument("--frontend-buffer", type=int, choices=range(2, 65), default=16 if command is snap else None, help="SDK raw frame buffer depth")
        command.add_argument("--backend-buffer", type=int, choices=range(2, 65), default=16 if command is snap else None, help="SDK processed frame buffer depth")
        command.add_argument("--usb-priority", type=int, choices=(0, 1, 2), default=2 if command is snap else None, help="SDK USB receive thread priority; excludes time-critical")
    snap.add_argument("--fps", type=positive_int, default=30, help="target rate recorded for compatibility; acquisition uses speed level")
    rec.add_argument("--encoder", choices=("auto", "libx264", "h264_nvenc", "hevc_nvenc", "libx265"))
    args = parser.parse_args(argv)
    if args.command == "probe":
        try:
            mii = sdk()
            print(json.dumps({"sdk_version": mii.Miicam.Version(),
                              "cameras": [describe(d, mii) for d in mii.Miicam.EnumV2()]}, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"Probe failed: {exc}", file=sys.stderr)
            return 1
    if args.command == 'record':
        try:
            resolve_record_args(args)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if args.dry_run:
            print(json.dumps({'source': args.profile_source, 'resolved': args.resolved_profile,
                              'storage_plan': args.storage_plan},
                             ensure_ascii=False, indent=2))
            return 0
    if args.gain > 65535:
        parser.error("--gain must fit a 16-bit SDK value")
    try:
        return run_capture(args)
    except Exception as exc:
        if getattr(args, 'control_dir', None):
            from operator_camera import atomic_write
            args.control_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(args.control_dir / 'status.json', json.dumps({'stage': 'incomplete', 'error': str(exc)}).encode('utf-8'))
        print(f'Capture could not start: {exc}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
