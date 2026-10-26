"""Local, offline entry point. Uses only the Python standard library."""
import datetime as dt
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

KIT = Path(__file__).resolve().parents[1]
# Embedded Python's isolated search path does not include this script folder.
sys.path.insert(0, str(KIT / 'scripts'))
from recording_profile import load_profile, resolve_record_args, storage_plan

PYTHON = KIT / 'runtime/python/python.exe'
CAPTURE = KIT / 'scripts/capture.py'
DEFAULTS = KIT / 'config/capture-defaults.json'
RECOMMENDED = KIT / 'config/mouse-4k45-recommended.json'
OFFICIAL_CRF = KIT / 'config/mouse-4k45-official-crf.json'
USER_CONFIG = KIT / '录制参数.toml'


def configured(short=False, check=False, config=None):
    """Use the editable file without interactive or hidden recording overrides."""
    config = Path(config).resolve() if config is not None else USER_CONFIG
    args = argparse.Namespace(config=config, duration=60 if short else None,
                              segment_seconds=20 if short else None, output_dir=None)
    resolve_record_args(args)  # Validate every field and storage budget before camera access.
    p = args.resolved_profile
    if p['encoder'] == 'auto':
        raise ValueError('统一入口要求明确填写 encoder；auto 会自动选择编码器，不能保证配置不回退。')
    preset = (p['nvenc_preset'] if p['encoder'].endswith('_nvenc') else
              p['x265_preset'] if p['encoder'] == 'libx265' else 'ultrafast（libx264 固定值）')
    quality = f"CQ {p['nvenc_cq']}" if p['encoder'].endswith('_nvenc') else f"CRF {p['crf']}"
    if p['rate_control'] != 'quality':
        quality = 'CQ / CRF 不生效（由目标码率和上限控制）'
    print(f'\n设置文件：{config}')
    print(f"{p['width']}×{p['height']}，{p['fps']} fps；编码器 {p['encoder']}；预设 {preset}")
    print(f"码率模式 {p['rate_control']}；生效质量参数 {quality}；GOP {p['gop_frames']} 帧；B 帧 {p['b_frames']}")
    if p['rate_control'] == 'capped_vbr':
        print(f"目标 {p['bitrate_mbps']:g} Mbps；上限 {p['maxrate_mbps']:g} Mbps；缓冲 {p['buffer_mbits']:g} Mbit")
    print(f"曝光 {p['exposure_us']/1000:g} ms；增益 {p['gain']/100:g}×（SDK {p['gain']}）")
    if p['preview_enabled']:
        print(f"录制预览已开启：最多 {p['preview_fps']} fps，最大宽度 {p['preview_max_width']} 像素；关闭预览窗口不停止录像。")
    else:
        print('录制预览已关闭；可在配置中设置 preview_enabled = true。')
    print(f"保存位置：{p['output_dir']}；时长 {args.duration:g} 秒（{p['duration_hours']:g} 小时）；每 {p['segment_seconds']} 秒分段")
    if p['status_enabled']:
        print(f"CMD进度：每 {p['status_interval_seconds']} 秒刷新；近期/平均码率按MKV文件增长估算。")
    else:
        print('CMD定期进度已关闭；可设置 status_enabled = true 开启。')
    if short:
        print('试录明确覆盖：duration_hours → 60 秒，segment_seconds → 20 秒；其他参数按文件执行。')
    else:
        print('保存位置、录制时长和分段长度均按文件执行，无交互覆盖。')
    if p['encoder'] == 'libx265' and p['fps'] >= 45:
        print('警告：本机 libx265 4K 45 fps 已出现编码吞吐不足；此配置仅供自选实验，不能视为已通过实时录制。')
    if p.get('imaging', {}).get('denoise', 'absent') is None:
        print('降噪：未控制或验证 SDK 降噪；本相机不支持该选项。')
    if args.storage_plan:
        print(f"启动前要求至少 {args.storage_plan['required_free_bytes']/1e9:.1f} GB 可用空间（含余量）。")
    if p['max_recording_gb'] is not None:
        print(f"总量保护阈值 {p['max_recording_gb']:g} GB；质量模式文件大小随画面变化。")
    arguments = ['record', '--config', str(config)]
    if short:
        arguments += ['--duration', '60', '--segment-seconds', '20']
    if check:
        print('配置检查通过；未打开相机、未测试编码器。实际录制仍需设备和编码器启动验证。')
        print('录制命令：' + subprocess.list2cmdline([str(PYTHON), '-B', '-X', 'utf8', str(CAPTURE), *arguments]))
        return 0
    return capture(arguments)


def default_output():
    # Keep videos out of the portable environment folder.
    if KIT.parent.name == 'tools' and KIT.parent.parent.name == 'private':
        return KIT.parent.parent / 'source/data/camera-recordings'
    return KIT.parent / 'camera-recordings'


def output_directory(default=None):
    default = Path(default) if default else default_output()
    raw = input(f'保存位置（直接回车使用 {default}）：').strip().strip('"')
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        raise ValueError('请填写绝对路径，例如 D:\\camera-recordings')
    path = path.resolve()
    if path == KIT or KIT in path.parents:
        raise ValueError('请把视频保存到环境包外，方便单独迁移软件。')
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture(arguments):
    print('拍摄前请关闭 ImageView 中的实时画面。Ctrl+C 可中止；请等待文件收尾。')
    process = subprocess.Popen([str(PYTHON), '-B', '-X', 'utf8', str(CAPTURE), *arguments], cwd=KIT)
    try:
        code = process.wait()
    except KeyboardInterrupt:
        # The child displays interruption and cleanup in its status block.
        # Parent output while it is redrawing would move the console cursor.
        code = process.wait()
    if code:
        print(f'本次未通过完整验收，退出码 {code}。若已产生记录，请查看 capture.json 和日志。')
    else:
        print('本次操作完成。长时间连续录制仍需按说明逐阶段验收。')
    return code


def preview():
    # Keep the vendor layout: x64 binaries resolve shared i18n/help one level up.
    executable = KIT / 'runtime/imageview/x64/imageview.exe'
    subprocess.Popen([str(executable)], cwd=executable.parent)
    print('已打开官方 ImageView。点击相机型号预览，再用软件内“录像”录制；调焦、光圈在镜头上调整。')


def recommended(short=False):
    print('这是旧 H.264 脚本配置；统一可编辑配置见菜单14至16。')
    settings, _ = load_profile(RECOMMENDED)
    print(f'\n设置文件：{RECOMMENDED}')
    print(f"{settings['width']}×{settings['height']}，{settings['fps']} fps；曝光 {settings['exposure_us']/1000:g} ms；增益 {settings['gain']/100:g}×")
    print(f"每 {settings['segment_seconds']} 秒分段；关键帧间隔 {settings['gop_frames']} 帧；B 帧 {settings['b_frames']}；编码器 {settings['encoder']}")
    if settings['rate_control'] == 'capped_vbr':
        print(f"码率：目标 {settings['bitrate_mbps']:g} Mbps，上限 {settings['maxrate_mbps']:g} Mbps；H.264 压缩，画质需试拍检查。")
    print('白平衡固定为配置值，需现场校准。该配置尚未通过 72 小时稳定性验收。')
    directory = output_directory(settings['output_dir'])
    if short:
        duration = 60
        print('使用同一配置试录 60 秒；其余相机与编码参数保持配置值。')
    else:
        raw = input(f"录制小时数（回车使用配置值 {settings['duration_hours']:g}）：").strip()
        hours = float(raw) if raw else settings['duration_hours']
        if not 0 < hours <= 168:
            raise ValueError('请输入大于 0 且不超过 168 的小时数。')
        duration = hours * 3600
    plan = storage_plan({**settings, 'duration_hours': duration / 3600})
    if plan:
        print(f"按目标码率视频约 {plan['target_video_bytes']/1e9:.1f} GB；启动前要求至少 {plan['required_free_bytes']/1e9:.1f} GB 可用空间（包含余量）。")
    if settings['max_recording_gb'] is not None:
        print(f"本次总量达到 {settings['max_recording_gb']:g} GB 保护阈值会报错停止，不覆盖旧文件。")
    print('输出按分段保存，并附 profile-used.json，记录配置来源、实际编码器及相机读回值。')
    print('请确认保存盘容量足够。录制期间会阻止系统自动睡眠；断电、重启和掉线仍会中断录制。')
    return capture(['record', '--config', str(RECOMMENDED), '--output-dir', str(directory), '--duration', str(duration)])


def official_crf(short=False):
    settings, _ = load_profile(OFFICIAL_CRF)
    print(f'\n设置文件：{OFFICIAL_CRF}')
    print(f"模拟官方样片：4K {settings['fps']} fps，x265 软件编码，CRF {settings['crf']}。")
    print('按样片提取的参数编码，仍需用实际小鼠画面检查画质；CPU需能持续完成编码。')
    directory = output_directory(settings['output_dir'])
    if short:
        duration, segment = 60, 20
        print('试录60秒，每20秒分段检查衔接。')
    else:
        raw = input('录制小时数（回车试录1小时）：').strip()
        hours = float(raw) if raw else 1
        if not 0 < hours <= 168:
            raise ValueError('请输入大于0且不超过168的小时数。')
        duration, segment = hours * 3600, settings['segment_seconds']
        print(f'每{segment/3600:g}小时保存一个片段；72小时连续稳定性尚未验收。')
    print(f"曝光{settings['exposure_us']/1000:g}ms，增益{settings['gain']/100:g}倍；画面效果还取决于现场光照和对焦。")
    print('CRF模式的文件大小随画面变化；质量参数以CRF为准，配置中的NVENC字段不生效。')
    print('输出包含视频、逐帧记录、profile-used.json与capture.json。')
    return capture(['record', '--config', str(OFFICIAL_CRF), '--output-dir', str(directory),
                    '--duration', str(duration), '--segment-seconds', str(segment)])


def record(diagnostic=False, short=False, fast=False):
    settings = json.loads(DEFAULTS.read_text(encoding='utf-8-sig'))
    directory = output_directory()
    if diagnostic:
        duration, fps = 30, 2
        print('USB2 诊断：4K、2 fps、30 秒，仅用于检查软件通路，不作为实验参数。')
    elif short:
        duration, fps = 60, (45 if fast else settings['fps'])
        print(f'4K {fps} fps 短测：60 秒、每 20 秒分段。需要 USB3 连接。')
    else:
        hours = float(input('录制小时数（例如 1 或 24）：'))
        if not 0 < hours <= 168:
            raise ValueError('请输入大于 0 且不超过 168 的小时数。')
        print('采集模式：1 4K 30 fps（默认）  2 4K 45 fps（本机已通过 5 分钟验收）')
        duration, fps = hours * 3600, (45 if input('选择 [1/2]：').strip() == '2' else 30)
    if fps == 45:
        print('本机已通过 4K 45 fps、5 分钟连续验收；1 小时、24 小时及客户电脑仍待验证。')
    print('编码器：1 软件编码（默认，跨电脑兼容）  2 NVIDIA 硬件编码（需支持的显卡）')
    encoder = 'h264_nvenc' if input('选择 [1/2]：').strip() == '2' else 'libx264'
    capture(['record', '--output-dir', str(directory), '--duration', str(duration),
             '--segment-seconds', str(20 if short or diagnostic else settings['segment_seconds']),
             '--fps', str(fps), '--exposure-us', str(settings['exposure_us']),
             '--gain', str(settings['gain']), '--encoder', encoder,
             '--speed-level', str(4 if fps == 45 else settings.get('speed_level', 2)),
             '--sdk-fps-limit', str(2 if diagnostic else settings.get('sdk_frame_rate_limit', 0)),
             '--frontend-buffer', str(settings.get('frontend_buffer', 16)),
             '--backend-buffer', str(settings.get('backend_buffer', 16)),
             '--usb-priority', str(settings.get('usb_thread_priority', 2))])


def main():
    if any(flag in sys.argv for flag in ('--configured', '--configured-test', '--configured-check')):
        try:
            return configured(short='--configured-test' in sys.argv,
                              check='--configured-check' in sys.argv)
        except (ValueError, OSError, KeyboardInterrupt) as exc:
            print(f'操作停止：{exc}')
            return 1
    if '--official-crf' in sys.argv or '--official-crf-test' in sys.argv:
        try:
            return official_crf(short='--official-crf-test' in sys.argv)
        except (ValueError, OSError, KeyboardInterrupt) as exc:
            print(f'操作停止：{exc}')
            return 1
    if '--preview' in sys.argv:
        preview()
        return
    if '--recommended' in sys.argv or '--recommended-test' in sys.argv:
        try:
            return recommended(short='--recommended-test' in sys.argv)
        except (ValueError, OSError, KeyboardInterrupt) as exc:
            print(f'操作停止：{exc}')
            return 1
    while True:
        print('\n相机拍摄工具  E3ISPM08300KPD + 8 mm\n'
              '1  官方 ImageView 预览（本机官方录像 NVENC 不兼容）\n'
              '2  检查相机型号、USB 连接速度和分辨率\n'
              '3  保存一张 4K 测试照片\n'
              '4  脚本：录制 60 秒 4K 30 fps 测试（含分段）\n'
              '5  脚本：按指定时长分段录像\n'
              '6  USB2 低帧率诊断（4K 2 fps 30 秒）\n'
              '7  校验环境包完整性\n'
              '8  打开使用说明\n'
              '9  脚本：录制 60 秒 4K 45 fps 测试\n'
              '10 原 H.264 脚本配置分段录制（默认 72 小时）\n'
              '11 原 H.264 脚本配置试录 60 秒\n'
              '12 模拟官方参数：CRF21 试录60秒（含分段）\n'
              '13 模拟官方参数：CRF21 每小时分段录制\n'
              '14 按录制参数.toml 启动录制（统一入口）\n'
              '15 按当前配置试录一分钟（60秒，每20秒分段）\n'
              '16 检查录制配置（不打开相机）\n'
              '17 客户电脑快速诊断（环境、编码器及20秒试录）\n'
              '0  退出')
        choice = input('请选择：').strip()
        try:
            if choice == '0':
                return
            if choice == '1':
                preview()
            elif choice == '2':
                capture(['probe'])
            elif choice == '3':
                path = output_directory() / ('snapshot-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.png')
                capture(['snapshot', '--output', str(path)])
            elif choice == '4':
                record(short=True)
            elif choice == '5':
                record()
            elif choice == '6':
                record(diagnostic=True)
            elif choice == '7':
                subprocess.run([str(PYTHON), '-X', 'utf8', str(KIT / 'scripts/verify-kit.py')], cwd=KIT)
            elif choice == '8':
                os.startfile(KIT / 'README.md')
            elif choice == '9':
                record(short=True, fast=True)
            elif choice == '10':
                recommended()
            elif choice == '11':
                recommended(short=True)
            elif choice == '12':
                official_crf(short=True)
            elif choice == '13':
                official_crf()
            elif choice == '14':
                configured()
            elif choice == '15':
                configured(short=True)
            elif choice == '16':
                configured(check=True)
            elif choice == '17':
                subprocess.run([str(PYTHON), '-B', '-X', 'utf8', str(KIT / 'scripts/diagnose_pc.py')], cwd=KIT)
        except (Exception, KeyboardInterrupt) as exc:
            print(f'操作停止：{exc}')


if __name__ == '__main__':
    sys.exit(main())
