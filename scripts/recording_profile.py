"""Validated, portable recording profiles. Reading a profile never opens a camera."""
import copy
import hashlib
import json
import math
import tomllib
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
FIELDS = {
    'schema_version', 'profile', 'description', 'width', 'height', 'fps',
    'duration_hours', 'output_dir', 'segment_seconds', 'encoder', 'gop_frames',
    'b_frames', 'crf', 'nvenc_cq', 'exposure_us', 'gain', 'speed_level',
    'sdk_frame_rate_limit', 'frontend_buffer', 'backend_buffer',
    'usb_thread_priority', 'keep_awake', 'imaging',
    'rate_control', 'bitrate_mbps', 'maxrate_mbps', 'buffer_mbits', 'max_recording_gb', 'nvenc_preset',
    'x265_preset', 'x265_params',
    'preview_enabled', 'preview_fps', 'preview_max_width',
    'status_enabled', 'status_interval_seconds',
}
RATE_DEFAULTS = {'rate_control': 'quality', 'bitrate_mbps': 24,
                 'maxrate_mbps': 26, 'buffer_mbits': 52, 'max_recording_gb': None,
                 'nvenc_preset': 'p4', 'x265_preset': 'superfast', 'x265_params': {},
                 'preview_enabled': False, 'preview_fps': 5, 'preview_max_width': 960,
                 'status_enabled': True, 'status_interval_seconds': 2}


def default_output():
    if KIT.parent.name == 'tools' and KIT.parent.parent.name == 'private':
        return KIT.parent.parent / 'source/data/camera-recordings'
    return KIT.parent / 'camera-recordings'


def integer(data, key, low, high):
    value = data[key]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{key} must be an integer in {low}..{high}')


def validate_profile(data):
    if not isinstance(data, dict):
        raise ValueError('Profile must be an object')
    unknown = set(data) - FIELDS
    required = FIELDS - {'description', 'imaging'} - set(RATE_DEFAULTS)
    missing = required - set(data)
    if unknown or missing:
        raise ValueError(f'Profile fields: unknown={sorted(unknown)}, missing={sorted(missing)}')
    p = copy.deepcopy(data)
    for key, value in RATE_DEFAULTS.items():
        p.setdefault(key, copy.deepcopy(value))
    if p['schema_version'] != 1 or type(p['schema_version']) is not int:
        raise ValueError('Only schema_version 1 is supported')
    if not isinstance(p['profile'], str) or not p['profile'].strip():
        raise ValueError('profile must be a nonempty name')
    if 'description' in p and not isinstance(p['description'], str):
        raise ValueError('description must be text')
    for key, low, high in (
        ('width', 3840, 3840), ('height', 2160, 2160), ('fps', 1, 60),
        ('segment_seconds', 1, 86400), ('gop_frames', 1, 65535), ('b_frames', 0, 3),
        ('crf', 0, 51), ('nvenc_cq', 1, 51), ('exposure_us', 1, 5000000),
        ('gain', 100, 65535), ('speed_level', 0, 4), ('sdk_frame_rate_limit', 0, 60),
        ('frontend_buffer', 2, 64), ('backend_buffer', 2, 64), ('usb_thread_priority', 0, 2),
        ('preview_fps', 1, 10), ('preview_max_width', 320, 1280),
        ('status_interval_seconds', 1, 60),
    ):
        integer(p, key, low, high)
    hours = p['duration_hours']
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 168:
        raise ValueError('duration_hours must be > 0 and <= 168')
    if p['encoder'] not in ('auto', 'libx264', 'h264_nvenc', 'hevc_nvenc', 'libx265'):
        raise ValueError('encoder must be auto, libx264, h264_nvenc, hevc_nvenc or libx265')
    if p['b_frames'] and p['encoder'] not in ('hevc_nvenc', 'libx265'):
        raise ValueError('b_frames above zero is supported only with HEVC encoders')
    if p['nvenc_preset'] not in tuple(f'p{i}' for i in range(1, 8)):
        raise ValueError('nvenc_preset must be p1..p7')
    if p['rate_control'] not in ('quality', 'capped_vbr'):
        raise ValueError('rate_control must be quality or capped_vbr')
    from x265_settings import X265_PRESETS, validate_x265_params
    if p['x265_preset'] not in X265_PRESETS:
        raise ValueError('Unsupported x265_preset')
    p['x265_params'] = validate_x265_params(p['x265_params'])
    if (p['encoder'] == 'libx265' and 'min-keyint' in p['x265_params']
            and int(p['x265_params']['min-keyint']) > p['gop_frames']):
        raise ValueError('x265 min-keyint must not exceed gop_frames')
    if p['encoder'] == 'libx265' and p['rate_control'] != 'quality':
        raise ValueError('libx265 currently supports CRF quality mode only')
    for key in ('bitrate_mbps', 'maxrate_mbps', 'buffer_mbits', 'max_recording_gb'):
        value = p[key]
        if key == 'max_recording_gb' and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1000000:
            raise ValueError(f'{key} must be a finite positive number')
    if p['maxrate_mbps'] < p['bitrate_mbps']:
        raise ValueError('maxrate_mbps must be >= bitrate_mbps')
    expected_speed = {30: 2, 45: 4}.get(p['fps'])
    if expected_speed is not None and p['speed_level'] != expected_speed:
        raise ValueError(f"For {p['fps']} fps on this camera, speed_level must be {expected_speed}")
    if type(p['keep_awake']) is not bool:
        raise ValueError('keep_awake must be true or false')
    if type(p['preview_enabled']) is not bool:
        raise ValueError('preview_enabled must be true or false')
    if type(p['status_enabled']) is not bool:
        raise ValueError('status_enabled must be true or false')
    if p['output_dir'] is not None:
        if not isinstance(p['output_dir'], str) or not Path(p['output_dir']).is_absolute():
            raise ValueError('output_dir must be null or an absolute folder path')
    destination = Path(p['output_dir']).resolve() if p['output_dir'] else default_output().resolve()
    if destination == KIT or KIT in destination.parents:
        raise ValueError('Recordings must be saved outside the portable kit')
    if 'imaging' in p:
        from camera_profile import validate_imaging
        p['imaging'] = validate_imaging(p['imaging'])
    return p


def storage_plan(profile):
    """Decimal bytes; reserve for CFR drift, container/CSV and 10 GiB free disk.

    This is a planning estimate based on the encoder ceiling, not proof that a
    recording has completed. The on-disk size guard is a separate last resort.
    """
    if profile['rate_control'] != 'capped_vbr':
        return None
    seconds = profile['duration_hours'] * 3600
    target = profile['bitrate_mbps'] * 1000000 / 8 * seconds
    ceiling = profile['maxrate_mbps'] * 1000000 / 8 * seconds
    # 5% acquisition/CFR clock headroom, 1% mux overhead and 256 bytes/frame CSV.
    estimated = math.ceil(ceiling * 1.05 * 1.01 + profile['fps'] * seconds * 1.05 * 256
                          + profile['buffer_mbits'] * 1000000 / 8 + 1024**2)
    return {'duration_seconds': seconds, 'target_video_bytes': math.ceil(target),
            'ceiling_video_bytes_at_nominal_fps': math.ceil(ceiling),
            'planned_recording_bytes_with_margin': estimated,
            'required_free_bytes': estimated + 10 * 1024**3,
            'max_recording_bytes': (math.floor(profile['max_recording_gb'] * 1000000000)
                                    if profile['max_recording_gb'] is not None else None),
            'note': 'Planning estimate; 5% CFR clock margin, 1% container margin, 256 bytes/frame index, VBV buffer, plus 10 GiB free disk'}


def load_profile(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    if path.suffix.lower() == '.toml':
        data = tomllib.loads(text)
        # TOML has no null. Keep these two representations equivalent to the
        # existing JSON schema, without filling in other required settings.
        if data.get('output_dir') == '':
            data['output_dir'] = None
        if isinstance(data.get('imaging'), dict):
            data['imaging'].setdefault('denoise', None)
    else:
        data = json.loads(text)
    return validate_profile(data), {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def legacy_profile():
    return {
        'schema_version': 1, 'profile': 'command-line', 'width': 3840, 'height': 2160,
        'fps': 30, 'duration_hours': 1, 'output_dir': None, 'segment_seconds': 300,
        'encoder': 'libx264', 'gop_frames': 60, 'b_frames': 0, 'crf': 18, 'nvenc_cq': 19,
        'exposure_us': 10000, 'gain': 100, 'speed_level': 2, 'sdk_frame_rate_limit': 0,
        'frontend_buffer': 16, 'backend_buffer': 16, 'usb_thread_priority': 2,
        'keep_awake': True,
    }


def resolve_record_args(args):
    profile, source = load_profile(args.config) if args.config else (legacy_profile(), None)
    if not args.config and (args.duration is None or args.output_dir is None):
        raise ValueError('record requires --config or both --duration and --output-dir')
    mapping = {
        'fps': 'fps', 'segment_seconds': 'segment_seconds', 'encoder': 'encoder',
        'gop_frames': 'gop_frames', 'b_frames': 'b_frames', 'crf': 'crf', 'nvenc_cq': 'nvenc_cq',
        'exposure_us': 'exposure_us', 'gain': 'gain', 'speed_level': 'speed_level',
        'sdk_fps_limit': 'sdk_frame_rate_limit', 'frontend_buffer': 'frontend_buffer',
        'backend_buffer': 'backend_buffer', 'usb_priority': 'usb_thread_priority',
        'rate_control': 'rate_control', 'bitrate_mbps': 'bitrate_mbps',
        'maxrate_mbps': 'maxrate_mbps', 'buffer_mbits': 'buffer_mbits',
        'max_recording_gb': 'max_recording_gb', 'nvenc_preset': 'nvenc_preset',
        'x265_preset': 'x265_preset', 'x265_params': 'x265_params',
        'preview_enabled': 'preview_enabled', 'preview_fps': 'preview_fps',
        'preview_max_width': 'preview_max_width',
        'status_enabled': 'status_enabled', 'status_interval_seconds': 'status_interval_seconds',
    }
    for argument, key in mapping.items():
        value = getattr(args, argument, None)
        if value is not None:
            profile[key] = value
    if getattr(args, 'fps', None) in (30, 45) and getattr(args, 'speed_level', None) is None:
        profile['speed_level'] = {30: 2, 45: 4}[args.fps]
    if not args.config and args.gop_frames is None:
        profile['gop_frames'] = profile['fps'] * 2
    if args.duration is not None:
        profile['duration_hours'] = args.duration / 3600
    if args.output_dir is not None:
        profile['output_dir'] = str(args.output_dir)
    profile = validate_profile(profile)
    for argument, key in mapping.items():
        setattr(args, argument, profile[key])
    args.duration = profile['duration_hours'] * 3600
    args.output_dir = Path(profile['output_dir']) if profile['output_dir'] else default_output()
    profile['output_dir'] = str(args.output_dir.resolve())
    args.imaging = profile.get('imaging')
    args.keep_awake = profile['keep_awake']
    args.resolved_profile = profile
    args.profile_source = source
    args.storage_plan = storage_plan(profile)
    if (args.storage_plan and args.storage_plan['max_recording_bytes'] is not None
            and args.storage_plan['planned_recording_bytes_with_margin'] > args.storage_plan['max_recording_bytes']):
        raise ValueError('Planned duration/rate exceeds max_recording_gb; shorten duration or lower bitrate/ceiling')
    return args
