"""Offline, bounded client-PC screening. Does not change recording settings."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'scripts'))
from recording_profile import resolve_record_args

PYTHON = KIT / 'runtime/python/python.exe'
FFMPEG = KIT / 'runtime/ffmpeg/bin/ffmpeg.exe'
FFPROBE = KIT / 'runtime/ffmpeg/bin/ffprobe.exe'
MIN_FREE = 10 * 1024**3


def default_report_root():
    if KIT.parent.name == 'tools' and KIT.parent.parent.name == 'private':
        return KIT.parent.parent / 'qa/scratch/camera-pc-diagnostics'
    return KIT.parent / 'camera-diagnostics'


def stop_owned_process(process):
    """Stop only this diagnostic's child tree, including its encoder/player."""
    if process.poll() is not None:
        return
    if sys.platform == 'win32':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def run_logged(command, path, timeout):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    result = {'returncode': None, 'timed_out': False, 'log': str(path)}
    with path.open('xb') as log:
        process = subprocess.Popen([str(v) for v in command], cwd=KIT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            result['returncode'] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
            stop_owned_process(process)
        except BaseException:
            stop_owned_process(process)
            raise
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    result['text'] = text
    return result


def probe_json(command, path, timeout):
    result = run_logged(command, path, timeout)
    if result['timed_out'] or result['returncode'] != 0:
        raise RuntimeError(f"检查超时或失败；见 {path.name}：{result['text'][-1000:]}")
    try:
        return json.loads(result['text'])
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f'检查结果无法解析；见 {path.name}') from exc


def check(report, name, status, detail):
    report['checks'].append({'name': name, 'status': status, 'detail': detail})
    print(f'[{status.upper()}] {name}：{detail}', flush=True)


def camera_assessment(probe, fps):
    cameras = probe.get('cameras', [])
    if not cameras:
        return 'warn', '未发现相机。电脑编码检查仍可进行；连接相机后重跑以验证USB和采集。', False
    if len(cameras) != 1:
        return 'warn', f'发现{len(cameras)}台相机；当前录制工具要求只连接一台。', False
    camera = cameras[0]
    if [3840, 2160] not in camera.get('resolutions', []):
        return 'fail', '当前相机未报告3840×2160，不能使用本工具的4K配置。', False
    usb = camera.get('usb', '未报告')
    if 'USB 2.0' in usb and fps >= 30:
        return 'fail', f'{usb}；请用USB3数据线直连USB3口后重跑。', False
    if not usb.startswith('USB 3.'):
        return 'warn', f"{camera.get('model')}，{usb}；连接速率未确认。", False
    return 'pass', f"{camera.get('model')}，{usb}，支持4K；实际带宽仍以试录为准。", True


def assess_recording(run_root, child):
    paths = list(run_root.glob('capture-*/capture.json'))
    result = {'status': 'fail', 'directory': str(run_root), 'log': child['log'],
              'detail': '没有可用的试录结果，请查看camera-recording.log。'}
    if len(paths) != 1:
        return result
    metadata = json.loads(paths[0].read_text(encoding='utf-8'))
    segments = metadata.get('segments', [])
    verified = bool(segments) and all(s.get('frames_decoded') == s.get('frames_submitted')
                                     and s.get('frames_decoded', 0) > 0 for s in segments)
    drops = [metadata.get(k) for k in ('sdk_sequence_gaps', 'sdk_reported_software_drops',
                                      'sdk_frontend_full_count', 'sdk_backend_full_count')]
    fps = metadata.get('measured_acquisition_fps')
    requested = metadata.get('requested_fps', 0)
    rate_ok = fps is not None and requested * .95 <= fps <= requested * 1.05
    passed = (not child['timed_out'] and child['returncode'] == 0
              and metadata.get('status') == 'complete' and verified and rate_ok
              and all(value == 0 for value in drops))
    result.update(status='pass' if passed else 'fail', metadata=str(paths[0]),
                  capture_status=metadata.get('status'), error=metadata.get('error'),
                  measured_fps=fps, frames=metadata.get('frames_captured'),
                  sequence_gaps=metadata.get('sdk_sequence_gaps'), decoded_segments_verified=verified)
    if passed:
        result['detail'] = f"20秒当前配置试录通过：{fps:.2f}fps，{metadata['frames_captured']}帧，{len(segments)}段逐段校验一致；不等于长期稳定性验收。"
        size = sum(p.stat().st_size for p in paths[0].parent.glob('*.mkv'))
        elapsed = metadata.get('capture_elapsed_s', 0)
        if elapsed > 0:
            result['sample_gb_per_hour'] = size / elapsed * 3600 / 1e9
    else:
        error = metadata.get('error') or ''
        if child['timed_out']:
            result['detail'] = '试录/收尾/校验超过120秒，诊断已结束自身的试录进程；查看日志，不算通过。'
        elif '0x0011' in error:
            # SDK miicam.h: MIICAM_EVENT_BACK_OVERFLOW = 0x0011.
            result['detail'] = 'SDK后端帧缓存溢出（0x0011），实际采集/处理链路未通过；先关闭其他负载后复测，不能仅凭此归因于显卡或相机损坏。'
        elif 'Encoder queue overflow' in error:
            result['detail'] = '编码输入队列堆满，送帧/编码处理未及时跟上；查看camera-recording.log中的每帧写入耗时与队列，暂不开始长录。'
        else:
            result['detail'] = error or '帧率、完整性、SDK丢帧或退出状态未通过，暂不作为可用录制方案。'
    return result


def encoder_failure_summary(row):
    if row.get('compiled') is False:
        return '随包FFmpeg未包含此编码器。'
    error = str(row.get('error') or '')
    if 'amfrt64.dll' in error.lower():
        reason = 'AMD编码运行库未能加载，可能没有相应显卡或驱动。'
    elif 'mfx' in error.lower():
        reason = 'Intel QSV会话未建立，核显/驱动/运行环境未通过本次测试。'
    elif row.get('status') == 'validation_failed':
        reason = '生成的小片未通过3帧解码校验。'
    else:
        reason = error.splitlines()[0][:180] if error else '未测试或未通过。'
    log = (row.get('capability', {}).get('probe_log') if row.get('status') == 'validation_failed'
           else row.get('log'))
    return reason + (f' 详见 {log}' if log else '')


def render_report(report):
    lines = ['相机录制电脑快速诊断', f"时间：{report['created_at']}",
             f"报告位置：{report['report_dir']}", f"诊断状态：{report.get('state', 'running')}", '',
             '录制用软编码/硬编码；播放用软解码/硬解码。本报告重点检查编码与采集。',
             '测试不修改录制参数、驱动、电源计划或现有录像；结果仅保存在本地。', '']
    env = report.get('environment', {})
    if env:
        system, cpu, memory, volume = (env.get(key, {}) for key in ('os', 'cpu', 'memory', 'volume'))
        def gib(value):
            return f'{value/1024**3:.1f} GiB' if value is not None else '未知'
        lines += [f"系统：{system.get('caption')}，{system.get('architecture')}，版本{system.get('version')}",
                  f"CPU：{cpu.get('name')}，{cpu.get('logical_processors')}个逻辑处理器",
                  f"内存：总计{gib(memory.get('total_bytes'))}，可用{gib(memory.get('available_bytes'))}",
                  f"保存盘：{volume.get('filesystem')}，类型{volume.get('drive_type')}，可用{gib(volume.get('free_bytes'))}"]
        lines += [f"显卡：{gpu.get('name')}，驱动{gpu.get('driver_version')}" for gpu in env.get('gpus', [])]
        lines.append('')
    lines += [f"[{c['status'].upper()}] {c['name']}：{c['detail']}" for c in report['checks']]
    lines += ['', '录制编码方式（运行通过≠4K实时通过）：']
    for row in report.get('encoders', []):
        support = '本工具可选' if row['supported_by_recorder'] else '仅硬件能力探测，本工具尚未接入'
        available = '小片编码及解码校验通过' if row.get('functional') else '未通过/不可用'
        bench = row.get('benchmark', {})
        speed = f"{bench['fps']:.1f}fps" if bench.get('fps') is not None else '无速度结果'
        bench_state = {'completed': '完成', 'timeout': '超时', 'failed': '失败', 'skipped': '未测试', 'incomplete': '未完成'}.get(bench.get('status'), '未知')
        label = row['label'].replace('Software', 'CPU软件').replace('HEVC', 'H.265')
        lines.append(f"- {label} [{row['encoder']}]：{support}；{available}；4K合成画面短测 {bench_state}，{speed}")
        if bench.get('error'):
            lines.append('  速度测试说明：' + str(bench['error'])[:1000])
        if not row.get('functional'):
            lines.append('  原因：' + encoder_failure_summary(row))
        if bench.get('settings_note'):
            lines.append('  参数：' + str(bench['settings_note']))
    camera = report.get('recording_test')
    if camera:
        lines += ['', '当前配置相机试录：' + camera['detail'], '试录路径：' + camera['directory']]
        if 'sample_gb_per_hour' in camera:
            lines.append(f"本次画面容量约{camera['sample_gb_per_hour']:.2f}GB/小时；CQ/CRF随画面变化，不能保证未来72小时容量。")
    lines += ['', '如何处理结果：',
              '1. 本工具直接支持libx264、libx265、h264_nvenc、hevc_nvenc。Intel QSV/AMD AMF即使探测通过，也不能直接填入当前录制配置。',
              '2. 小片通过只说明驱动与编码器能工作。合成4K短测包括启动/滤镜成本，不包含相机USB采集、Python送帧和写盘；速度达标也不等于真实45fps稳定。',
              '3. 当前配置试录不通过时，先看失败原因及日志。不要因有显卡或小片成功，就直接开始无人值守长录。',
              '4. 调整录制参数.toml后重新诊断；建议先用“试录当前配置一分钟.cmd”检查实际小鼠画质与分段，再逐步延长。',
              '5. 磁盘小文件测试受缓存影响，只能排查权限、空间和明显异常，不证明持续写入性能。',
              '6. 保存本目录的report.txt、report.json及日志便于定位；不会自动上传。', '']
    return '\n'.join(lines)


def save_report(report):
    directory = Path(report['report_dir'])
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (directory / 'report.txt').write_text(render_report(report), encoding='utf-8-sig')


def run_diagnostics(args, report):
    directory = Path(report['report_dir'])
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        check(report, '运行平台', 'fail', '需要Windows x64与随包64位Python。')
        return
    check(report, '运行平台', 'pass', 'Windows 64位Python运行正常。')
    resolved = argparse.Namespace(config=args.config, duration=None, output_dir=args.output_dir)
    try:
        resolve_record_args(resolved)
        profile = resolved.resolved_profile
        report['profile'] = profile
        report['profile_source'] = resolved.profile_source
        target = resolved.output_dir.resolve()
        check(report, '录制配置', 'pass', f"{profile['encoder']}，{profile['fps']}fps，保存到{target}；原配置不修改。")
    except (ValueError, OSError) as exc:
        check(report, '录制配置', 'fail', str(exc))
        return
    print('正在读取系统、显卡驱动、保存卷与电源状态……', flush=True)
    env = probe_json([PYTHON, '-B', '-X', 'utf8', KIT / 'scripts/pc_environment.py',
                      '--environment', target], directory / 'environment.log', 30)
    report['environment'] = env
    for error in env.get('errors', []):
        check(report, '系统信息', 'warn', str(error))
    active = env.get('active_recording_processes', [])
    if active:
        check(report, '相机或编码占用', 'warn', '检测到正在运行的相机/编码软件：' + str(active)
              + '。本次跳过压力测试和相机试录；请停止自己的录制并关闭ImageView后重跑。')
        return
    if any('process' in str(e).lower() or '进程' in str(e)
           or 'windows_inventory' in str(e).lower() for e in env.get('errors', [])):
        check(report, '占用检测', 'warn', '无法确认进程占用，本次不启动压力测试。')
        return
    memory = env.get('memory', {}).get('available_bytes')
    if memory is not None and memory < 2 * 1024**3:
        check(report, '内存余量', 'warn', '可用内存不足2GiB，建议关闭无关软件后实测；这不是硬件最低要求判定。')
    if env.get('power', {}).get('ac_connected') is False:
        check(report, '供电', 'warn', '当前未接交流电，请接电源后录制；不会自动改变电源设置。')
    report['power_note'] = '电源方案与睡眠设置见environment.log；工具录制时会阻止自动睡眠，不能防止断电或系统更新重启。'
    check(report, '电源设置', 'info', report['power_note'])
    print('正在校验软件包完整性……', flush=True)
    integrity = run_logged([PYTHON, '-B', '-X', 'utf8', KIT / 'scripts/verify-kit.py'],
                           directory / 'integrity.log', 90)
    if integrity['returncode'] != 0 or integrity['timed_out']:
        check(report, '软件包完整性', 'fail', '文件缺失、变更或校验超时；见integrity.log。重新完整复制软件包后再测试。')
        return
    check(report, '软件包完整性', 'pass', '固定文件校验通过；可编辑配置单独校验。')
    disk_ok = False
    try:
        target.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target).free < MIN_FREE:
            raise RuntimeError('可用空间不足10GiB预留，已跳过磁盘写入测试。')
        disk = probe_json([PYTHON, '-B', '-X', 'utf8', KIT / 'scripts/pc_environment.py', '--disk', target],
                          directory / 'disk.log', 20)
        report['disk_test'] = disk
        disk_ok = bool(disk.get('ok'))
        speed = disk.get('mib_per_s')
        check(report, '保存盘写入', 'pass' if disk_ok else 'fail',
              f"小文件写入/flush/fsync约{speed:.1f}MiB/s；仅快速筛查。" if disk_ok and speed is not None
              else str(disk.get('error') or '未测得写入速度，见disk.log'))
        if disk.get('cleanup_error'):
            check(report, '临时探针清理', 'warn', str(disk['cleanup_error']))
    except (OSError, RuntimeError) as exc:
        check(report, '保存盘写入', 'fail', str(exc) + '；超时时可能留下以探针前缀命名的临时文件，不影响已有录像。')
    volume = env.get('volume', {})
    free = volume.get('free_bytes')
    if free is None:
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            pass
    needed = max(MIN_FREE, resolved.storage_plan['required_free_bytes'] if resolved.storage_plan else MIN_FREE)
    if free is None or free < needed:
        check(report, '保存盘容量', 'fail', f'可用空间{free}字节；当前计划启动需至少{needed/1e9:.1f}GB。')
        disk_ok = False
    else:
        check(report, '保存盘容量', 'pass', f'可用{free/1e9:.1f}GB；启动门槛满足。CQ/CRF模式总量需按实际画面估算。')
    if str(volume.get('filesystem', '')).upper() in ('FAT', 'FAT32'):
        check(report, '文件系统', 'fail', 'FAT/FAT32有单文件大小限制，不适合每小时4K分段；选择NTFS或exFAT保存盘。')
        disk_ok = False
    elif volume.get('drive_type') in (4, 'Network', 'network'):
        check(report, '保存位置', 'warn', '网络保存位置可能受连接波动影响，需要单独验证长时间写入。')
    save_report(report)
    camera_ready = False
    try:
        probe = probe_json([PYTHON, '-B', '-X', 'utf8', KIT / 'scripts/capture.py', 'probe'],
                           directory / 'camera-probe.log', 20)
        report['camera_probe'] = probe
        status, detail, camera_ready = camera_assessment(probe, profile['fps'])
        check(report, '相机与USB', status, detail)
    except (OSError, RuntimeError) as exc:
        check(report, '相机与SDK', 'fail', str(exc))
    print('正在逐项测试编码器；没有相应显卡时显示不可用属于正常结果。', flush=True)
    from encoder_diagnostics import probe_encoders
    report['encoders'] = probe_encoders(FFMPEG, FFPROBE, profile, directory, progress=lambda message: print(message, flush=True))
    functional = [r['encoder'] for r in report['encoders'] if r.get('functional') and r['supported_by_recorder']]
    check(report, '可运行的录制编码器', 'pass' if functional else 'fail', ', '.join(functional) or '没有通过实际编码及解码检查的录制编码器。')
    selected = next((r for r in report['encoders'] if r['encoder'] == profile['encoder']), None)
    if not selected or not selected.get('functional'):
        check(report, '当前配置编码器', 'fail', f"{profile['encoder']}未通过运行检查；不会自动切换或修改配置。")
    elif selected.get('benchmark', {}).get('meets_target') is not True:
        check(report, '当前编码器4K速度', 'warn', '合成短测未确认达到目标帧率；包含启动开销，以相机试录为进一步检查。')
    else:
        check(report, '当前编码器4K速度', 'pass', '合成短测达到目标帧率，仍需要完整相机链路检查。')
    save_report(report)
    if args.skip_camera or not camera_ready or not disk_ok or profile['encoder'] not in functional:
        check(report, '20秒相机试录', 'skipped', '按参数跳过，或相机/USB/保存盘/当前编码器条件不足；环境检查不代表实际采集已通过。')
        return
    run_root = target / 'diagnostics' / directory.name
    run_root.mkdir(parents=True, exist_ok=False)
    snapshot = directory / 'profile-tested.json'
    snapshot.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'开始20秒相机试录（每10秒分段），按当前曝光/增益/编码/预览配置；视频保存在{run_root}', flush=True)
    child = run_logged([PYTHON, '-B', '-X', 'utf8', KIT / 'scripts/capture.py', 'record',
                        '--config', snapshot, '--duration', '20', '--segment-seconds', '10', '--output-dir', run_root],
                       directory / 'camera-recording.log', 120)
    report['recording_test'] = assess_recording(run_root, child)
    check(report, '20秒相机试录', report['recording_test']['status'], report['recording_test']['detail'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=KIT / '录制参数.toml')
    parser.add_argument('--output-dir', type=Path, help='Test recording destination without modifying the configuration')
    parser.add_argument('--report-root', type=Path, default=default_report_root())
    parser.add_argument('--skip-camera', action='store_true', help='Skip the 20-second real camera recording')
    args = parser.parse_args(argv)
    name = 'diagnostic-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    try:
        directory = args.report_root.resolve() / name
        if directory == KIT or KIT in directory.parents:
            parser.error('诊断报告必须放在软件包外，以免影响软件包校验。')
        directory.mkdir(parents=True, exist_ok=False)
    except OSError:
        directory = Path(tempfile.mkdtemp(prefix=name + '-'))
    report = {'schema_version': 1, 'created_at': dt.datetime.now().astimezone().isoformat(),
              'report_dir': str(directory), 'state': 'running', 'checks': [], 'encoders': []}
    print('客户电脑快速诊断：离线运行，无需管理员权限。通常约2～4分钟，慢电脑可能更久。', flush=True)
    print(f'报告保存在：{directory}\n请先退出ImageView及其他录像程序；诊断不会关闭它们。', flush=True)
    save_report(report)
    try:
        run_diagnostics(args, report)
        report['state'] = 'finished'
    except KeyboardInterrupt:
        report['state'] = 'interrupted'
        check(report, '诊断中止', 'warn', '用户中止；已保留完成部分的报告和日志，未测试项不能视为通过。')
    except Exception as exc:
        report['state'] = 'error'
        check(report, '诊断异常', 'fail', f'{type(exc).__name__}: {exc}')
    finally:
        save_report(report)
    print('\n' + render_report(report), flush=True)
    print(f"\n请打开报告：{directory / 'report.txt'}", flush=True)
    if any(c['status'] == 'fail' for c in report['checks']) or report['state'] == 'error':
        return 1
    if report['state'] != 'finished' or report.get('recording_test', {}).get('status') != 'pass':
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
