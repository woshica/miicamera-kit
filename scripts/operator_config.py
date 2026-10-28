"""Persistent operator settings; no camera access and no import-time writes."""
import copy
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid

from recording_profile import KIT, load_profile, validate_profile
from camera_profile import validate_imaging

_LOCK = threading.RLock()
_DAILY = {'output_dir', 'duration_hours', 'segment_minutes', 'scheme'}
_REMOVED = {'output_dir', 'duration_hours', 'segment_seconds', 'exposure_us', 'gain'}


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f'{label}字段不正确')


def _scaled(value, factor, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label}必须是有限数字')
    scaled = value * factor
    if not math.isfinite(scaled) or abs(scaled - round(scaled)) > 1e-7:
        raise ValueError(f'{label}精度超出支持范围')
    return round(scaled)


def _write(path, value):
    """Replace one JSON file atomically, with durable staged contents."""
    fd, temporary = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class OperatorConfig:
    def __init__(self, kit=KIT):
        self.kit = Path(kit).resolve()
        self.directory = self.kit / 'config' / 'operator'

    def initialize(self):
        with _LOCK:
            if not self.directory.exists():
                source = self.kit / '录制参数.toml'
                if not source.exists():
                    source = self.kit / 'config' / 'operator-template.json'
                profile, _ = load_profile(source)
                destination = profile['output_dir']
                if not destination:
                    destination = (self.kit.parent.parent / 'source/data/camera-recordings'
                                   if self.kit.parent.name == 'tools' and self.kit.parent.parent.name == 'private'
                                   else self.kit.parent / 'camera-recordings')
                daily = dict(output_dir=str(Path(destination).resolve()),
                             duration_hours=profile['duration_hours'],
                             segment_minutes=profile['segment_seconds'] / 60, scheme='current')
                schemes = {'current': dict(name='现场当前方案', exposure_ms=profile['exposure_us'] / 1000,
                                           gain_multiplier=profile['gain'] / 100, overrides={})}
                advanced = {k: v for k, v in profile.items() if k not in _REMOVED}
                self._validate(advanced, daily, schemes)
                self.directory.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix='.operator-', dir=self.directory.parent))
                try:
                    for name, data in [('advanced', advanced), ('daily', daily), ('schemes', schemes)]:
                        _write(stage / (name + '.json'), data)
                    os.rename(stage, self.directory)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            return self.view()

    def _read(self, name):
        try:
            return json.loads((self.directory / name).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError(f'无法读取操作配置 {name}：{exc}') from exc

    def _state(self):
        # A pending journal means the prior multi-file commit did not complete.
        journal = self.directory / '.transaction.json'
        if journal.exists():
            previous = self._read(journal.name)
            for name in ('daily', 'schemes'):
                _write(self.directory / (name + '.json'), previous[name])
            journal.unlink()
        advanced, daily, schemes = (self._read(name + '.json') for name in ('advanced', 'daily', 'schemes'))
        self._validate(advanced, daily, schemes)
        return advanced, daily, schemes

    def _merged(self, advanced, daily, scheme):
        profile = copy.deepcopy(advanced)
        profile.update(copy.deepcopy(scheme['overrides']))
        profile.update(output_dir=daily['output_dir'], duration_hours=daily['duration_hours'],
                       segment_seconds=_scaled(daily['segment_minutes'], 60, '分段分钟数'),
                       exposure_us=_scaled(scheme['exposure_ms'], 1000, '曝光毫秒数'),
                       gain=_scaled(scheme['gain_multiplier'], 100, '增益倍数'))
        if profile.get('encoder') == 'auto':
            raise ValueError('请选择明确的编码器，不能使用 auto')
        try:
            return validate_profile(profile)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ValueError(f'录制参数无效：{exc}') from exc

    def _validate(self, advanced, daily, schemes):
        _keys(daily, _DAILY, '日常设置')
        if not isinstance(advanced, dict) or set(advanced) & _REMOVED:
            raise ValueError('高级配置包含不允许的日常设置')
        output = daily['output_dir']
        if not isinstance(output, str) or not output.strip() or '\x00' in output or not Path(output).is_absolute():
            raise ValueError('保存位置必须是完整的绝对文件夹路径')
        destination = Path(output).resolve()
        if destination == self.kit or self.kit in destination.parents:
            raise ValueError('录像必须保存在工具包以外的文件夹')
        if destination.exists() and not destination.is_dir():
            raise ValueError('保存位置不能是文件')
        if not isinstance(schemes, dict) or not schemes:
            raise ValueError('至少需要一个拍摄方案')
        if not isinstance(daily['scheme'], str) or daily['scheme'] not in schemes:
            raise ValueError('请选择已有拍摄方案')
        names = set()
        for key, scheme in schemes.items():
            if not isinstance(key, str) or not key:
                raise ValueError('方案编号无效')
            _keys(scheme, {'name', 'exposure_ms', 'gain_multiplier', 'overrides'}, '拍摄方案')
            if not isinstance(scheme['name'], str) or not scheme['name'].strip() or len(scheme['name']) > 100:
                raise ValueError('方案名称须为 1 至 100 个字符')
            normalized_name = scheme['name'].strip()
            if normalized_name in names:
                raise ValueError('方案名称已存在，请使用不同名称')
            names.add(normalized_name)
            overrides = scheme['overrides']
            allowed = (set(advanced) | {'imaging'}) - {'schema_version', 'profile'}
            if not isinstance(overrides, dict) or set(overrides) - allowed:
                raise ValueError('方案包含不允许覆盖的高级字段')
            self._merged(advanced, daily, scheme)

    def _commit(self, advanced, old_daily, old_schemes, daily, schemes):
        self._validate(advanced, daily, schemes)
        journal = self.directory / '.transaction.json'
        _write(journal, {'daily': old_daily, 'schemes': old_schemes})
        try:
            _write(self.directory / 'daily.json', daily)
            _write(self.directory / 'schemes.json', schemes)
            journal.unlink()
        except BaseException:
            # Leave the journal for startup recovery if rollback itself fails.
            _write(self.directory / 'daily.json', old_daily)
            _write(self.directory / 'schemes.json', old_schemes)
            journal.unlink()
            raise

    def view(self):
        with _LOCK:
            advanced, daily, schemes = self._state()
            profile = self._merged(advanced, daily, schemes[daily['scheme']])
            return dict(daily=daily, schemes=schemes,
                        imaging=copy.deepcopy(profile.get('imaging')),
                        summary=dict(width=profile['width'], height=profile['height'],
                                     resolution=f"{profile['width']} × {profile['height']}",
                                     fps=profile['fps'], encoder=profile['encoder']),
                        advanced_path=str(self.directory / 'advanced.json'))

    def save(self, daily, scheme_values=None):
        with _LOCK:
            advanced, old_daily, old_schemes = self._state()
            daily, schemes = copy.deepcopy(daily), copy.deepcopy(old_schemes)
            _keys(daily, _DAILY, '日常设置')
            if scheme_values is not None:
                _keys(scheme_values, {'exposure_ms', 'gain_multiplier'}, '拍摄参数')
                if not isinstance(daily['scheme'], str) or daily['scheme'] not in schemes:
                    raise ValueError('请选择已有拍摄方案')
                schemes[daily['scheme']].update(copy.deepcopy(scheme_values))
            self._commit(advanced, old_daily, old_schemes, daily, schemes)
            return self.view()

    def save_imaging(self, imaging, daily=None, scheme_values=None):
        """Atomically save complete imaging and the form values for its scheme."""
        with _LOCK:
            advanced, old_daily, old_schemes = self._state()
            imaging = validate_imaging(imaging)
            daily = copy.deepcopy(old_daily if daily is None else daily)
            _keys(daily, _DAILY, '日常设置')
            if not isinstance(daily['scheme'], str) or daily['scheme'] not in old_schemes:
                raise ValueError('请选择已有拍摄方案')
            schemes = copy.deepcopy(old_schemes)
            selected = schemes[daily['scheme']]
            if scheme_values is not None:
                _keys(scheme_values, {'exposure_ms', 'gain_multiplier'}, '拍摄参数')
                selected.update(copy.deepcopy(scheme_values))
            selected['overrides']['imaging'] = imaging
            self._commit(advanced, old_daily, old_schemes, daily, schemes)
            return self.view()

    def create_scheme(self, name, source_id, scheme_values=None, daily=None):
        with _LOCK:
            advanced, old_daily, old_schemes = self._state()
            if not isinstance(source_id, str) or source_id not in old_schemes:
                raise ValueError('请选择要复制的已有方案')
            daily = copy.deepcopy(old_daily if daily is None else daily)
            _keys(daily, _DAILY, '日常设置')
            schemes = copy.deepcopy(old_schemes)
            key = uuid.uuid4().hex
            schemes[key] = copy.deepcopy(schemes[source_id])
            # Capture effective imaging, including values inherited from advanced.
            source_profile = self._merged(advanced, old_daily, schemes[source_id])
            if 'imaging' in source_profile:
                schemes[key]['overrides']['imaging'] = copy.deepcopy(source_profile['imaging'])
            schemes[key]['name'] = name.strip() if isinstance(name, str) else name
            if scheme_values is not None:
                _keys(scheme_values, {'exposure_ms', 'gain_multiplier'}, '拍摄参数')
                schemes[key].update(copy.deepcopy(scheme_values))
            daily['scheme'] = key
            self._commit(advanced, old_daily, old_schemes, daily, schemes)
            return self.view()

    def resolved(self, short=False):
        with _LOCK:
            advanced, daily, schemes = self._state()
            profile = self._merged(advanced, daily, schemes[daily['scheme']])
            if short:
                profile.update(duration_hours=60 / 3600, segment_seconds=20)
            return validate_profile(profile)
