"""Offline matched-content NVENC size/SSIM comparison of an existing video.

The input is an already compressed reference, not raw camera output. Outputs
are unique, non-overwriting files in the selected scratch directory.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run(command, log_path=None):
    completed = subprocess.run(command, capture_output=True, text=True)
    if log_path:
        log_path.write_text(completed.stderr, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise RuntimeError(f"Command failed ({completed.returncode}): {shlex.join(map(str, command))}\n"
                           f"See {log_path or 'stderr'}: {completed.stderr[-1200:]}")
    return completed.stdout


def probe(ffprobe, path, count=False):
    command = [str(ffprobe), "-v", "error"]
    if count:
        command.append("-count_frames")
    command += ["-select_streams", "v:0", "-show_entries",
                "format=duration,size,bit_rate:stream=codec_name,width,height,"
                "start_time,time_base,avg_frame_rate,nb_frames,nb_read_frames,has_b_frames",
                "-of", "json", str(path)]
    return json.loads(run(command))


def frame_count(info):
    video = info["streams"][0]
    value = video.get("nb_read_frames") or video.get("nb_frames")
    return int(value) if value and value != "N/A" else None


def encode_command(ffmpeg, source, output, spec):
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-n", "-i", str(source),
               "-map", "0:v:0", "-an", "-fps_mode", "passthrough", "-c:v", spec["codec"],
               "-preset", spec["preset"], "-rc", "vbr"]
    if "cq" in spec:
        command += ["-b:v", "0", "-cq", str(spec["cq"])]
    else:
        command += ["-b:v", "24M", "-maxrate", "26M", "-bufsize", "52M"]
    command += ["-pix_fmt", "yuv420p", "-g", str(spec["gop"]),
                "-bf", str(spec["b_frames"]), "-forced-idr", "1",
                "-no-scenecut", "1", "-rc-lookahead", "0", "-f", "matroska", str(output)]
    return command


def ssim(ffmpeg, source, encoded, log_path, frames):
    # Pair by decoded frame ordinal. MP4 and MKV timestamps have different precision.
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(source),
               "-i", str(encoded), "-filter_complex",
               f"[0:v]trim=end_frame={frames},settb=1/45,setpts=N[a];"
               f"[1:v]trim=end_frame={frames},settb=1/45,setpts=N[b];"
               "[a][b]ssim=stats_file=-", "-frames:v", str(frames),
               "-fps_mode", "passthrough", "-an", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True)
    log_path.write_text(result.stderr + "\n" + result.stdout,
                        encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"SSIM failed ({result.returncode}); see {log_path}")
    matches = re.findall(r"All:([0-9.]+) \(([0-9.]+)\)", result.stderr)
    if not matches:
        raise RuntimeError(f"SSIM summary absent; see {log_path}")
    observed = len(re.findall(r"(?m)^n:\s*\d+", result.stdout))
    if observed != frames:
        raise RuntimeError(f"Expected {frames} SSIM frame records, got {observed}; see {log_path}")
    return {"all": float(matches[-1][0]), "db": float(matches[-1][1]),
            "reference": "decoded already-compressed official recording",
            "window": f"first {frames} decoded frames by ordinal (~{frames / 45:.2f} s nominal)",
            "frames": observed, "command": command}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--ssim-frames", type=int, default=450)
    parser.add_argument("--refresh-ssim", action="store_true",
                        help="Recompute SSIM for existing completed encodes without overwriting video")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    results_path = args.output_dir / "results.json"
    if args.refresh_ssim:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        if source != Path(data["source"]["path"]):
            parser.error("source differs from results.json")
        for trial in data["trials"]:
            if trial.get("decoded_frames") != data["source"]["decoded_frames"]:
                trial["status"] = "failed"
                trial["error"] = "Decoded frame count differs from source"
                continue
            name = trial["spec"]["name"]
            trial["ssim"] = ssim(args.ffmpeg, source, Path(trial["output"]),
                                 args.output_dir / f"{name}.ssim.log", args.ssim_frames)
            trial["status"] = "complete"
            results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{name}: refreshed SSIM {trial['ssim']['all']:.6f}", flush=True)
        return 0 if all(t["status"] == "complete" for t in data["trials"]) else 1
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("output_dir must be absent or empty; comparison never overwrites artifacts")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_info = probe(args.ffprobe, source, count=True)
    source_count = frame_count(source_info)
    specs = [
        {"name": "legacy_h264_24m", "codec": "h264_nvenc", "preset": "p4", "gop": 45, "b_frames": 0},
        *({"name": f"hevc_cq{cq}", "codec": "hevc_nvenc", "preset": "p5",
           "gop": 250, "b_frames": 3, "cq": cq} for cq in (20, 23, 26)),
    ]
    data = {"created_utc": datetime.now(timezone.utc).isoformat(),
            "source": {"path": str(source), "probe": source_info,
                       "decoded_frames": source_count}, "trials": []}
    for spec in specs:
        output = args.output_dir / f"{spec['name']}.mkv"
        command = encode_command(args.ffmpeg, source, output, spec)
        trial = {"spec": spec, "output": str(output), "encode_command": command}
        data["trials"].append(trial)
        results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            run(command, args.output_dir / f"{spec['name']}.encode.log")
            trial["probe"] = probe(args.ffprobe, output, count=True)
            trial["decoded_frames"] = frame_count(trial["probe"])
            trial["frame_count_matches_source"] = trial["decoded_frames"] == source_count
            if not trial["frame_count_matches_source"]:
                raise RuntimeError("Decoded frame count differs from source")
            trial["size_ratio_to_source"] = int(trial["probe"]["format"]["size"]) / int(source_info["format"]["size"])
            trial["ssim"] = ssim(args.ffmpeg, source, output,
                                 args.output_dir / f"{spec['name']}.ssim.log", args.ssim_frames)
            trial["status"] = "complete"
        except Exception as exc:
            trial["status"] = "failed"
            trial["error"] = str(exc)
        results_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{spec['name']}: {trial['status']}", flush=True)
    print("name\tMiB\tratio\tframes\tSSIM (decoded source)")
    for trial in data["trials"]:
        if trial["status"] == "complete":
            print(f"{trial['spec']['name']}\t{int(trial['probe']['format']['size']) / 1048576:.2f}\t"
                  f"{trial['size_ratio_to_source']:.3f}\t{trial['decoded_frames']}\t{trial['ssim']['all']:.6f}")
    return 0 if all(t["status"] == "complete" and t["frame_count_matches_source"]
                    for t in data["trials"]) else 1


if __name__ == "__main__":
    sys.exit(main())
