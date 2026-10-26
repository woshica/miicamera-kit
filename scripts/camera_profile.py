"""Apply and verify the imaging portion of a recording profile.

The bundled Miicam Python wrapper raises HRESULTException for failed SDK calls.
Call this from the controlling thread, outside SDK image/event callbacks, before
recording starts. A failed application may have changed earlier settings.
"""

from copy import deepcopy


FIELDS = (
    'bit_depth', 'binning', 'roi', 'gamma', 'contrast', 'brightness',
    'hue', 'saturation', 'sharpening', 'denoise', 'white_balance_temp',
    'white_balance_tint', 'level_low', 'level_high',
)

RANGES = {
    'gamma': (20, 180),
    'contrast': (-255, 255),
    'brightness': (-255, 255),
    'hue': (-180, 180),
    'saturation': (0, 255),
    'sharpening': (0, 500),
    'white_balance_temp': (2000, 15000),
    'white_balance_tint': (200, 2500),
}


class ImagingApplyError(RuntimeError):
    """A requested value could not be proven effective on this camera."""

    def __init__(self, field, operation, detail):
        self.field = field
        self.operation = operation
        super().__init__(f'{field}: {operation}: {detail}')


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')


def validate_imaging(settings):
    """Validate the complete, exact schema without opening a camera."""
    if not isinstance(settings, dict):
        raise ValueError('imaging must be a JSON object')
    unknown = set(settings) - set(FIELDS)
    missing = set(FIELDS) - set(settings)
    if unknown or missing:
        raise ValueError(f'imaging fields: unknown={sorted(unknown)}, missing={sorted(missing)}')
    if type(settings['bit_depth']) is not int or settings['bit_depth'] != 8:
        raise ValueError('bit_depth supports only 8')
    if type(settings['binning']) is not int or settings['binning'] != 1:
        raise ValueError('binning supports only 1 (no binning)')
    if settings['roi'] != 'full':
        raise ValueError("roi supports only 'full'")
    for name, (low, high) in RANGES.items():
        _integer(settings[name], name, low, high)
    if settings['denoise'] is not None:
        _integer(settings['denoise'], 'denoise', 0, 100)
    for name in ('level_low', 'level_high'):
        values = settings[name]
        if type(values) is not list or len(values) != 4:
            raise ValueError(f'{name} must have four integer entries')
        for index, value in enumerate(values):
            _integer(value, f'{name}[{index}]', 0, 255)
    for index, (low, high) in enumerate(zip(settings['level_low'], settings['level_high'])):
        if low >= high:
            raise ValueError(f'level_low[{index}] must be less than level_high[{index}]')
    return deepcopy(settings)


def _call(field, operation, function, *args):
    try:
        return function(*args)
    except Exception as exc:
        raise ImagingApplyError(field, operation, f'{type(exc).__name__}: {exc}') from exc


def _set_and_check(field, setter, getter, expected):
    _call(field, 'write', setter)
    actual = _call(field, 'readback', getter)
    if actual != expected:
        raise ImagingApplyError(field, 'readback', f'requested {expected!r}, got {actual!r}')
    return actual


def diagnose_imaging_support(camera, mii):
    """Read-only probe of profile getters; never writes or triggers auto modes.

    A successful getter cannot guarantee its setter is supported, so the final
    application still needs write/readback verification.
    """
    probes = {
        'bit_depth': lambda: camera.get_Option(mii.MIICAM_OPTION_BITDEPTH),
        'binning': lambda: camera.get_Option(mii.MIICAM_OPTION_BINNING),
        'roi': camera.get_Roi,
        'gamma': camera.get_Gamma,
        'contrast': camera.get_Contrast,
        'brightness': camera.get_Brightness,
        'hue': camera.get_Hue,
        'saturation': camera.get_Saturation,
        'sharpening': lambda: camera.get_Option(mii.MIICAM_OPTION_SHARPENING),
        'denoise': lambda: camera.get_Option(mii.MIICAM_OPTION_DENOISE),
        'white_balance_temp/white_balance_tint': camera.get_TempTint,
        'level_low/level_high': camera.get_LevelRange,
    }
    results = {}
    for field, getter in probes.items():
        supported, value = _probe(getter)
        results[field] = ({'supported_read': True, 'value': value} if supported
                          else {'supported_read': False, 'error': value})
    return results


def _probe(getter):
    try:
        return True, getter()
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def apply_imaging(camera, mii, settings):
    """Write every requested setting and return the verified effective values.

    An SDK E_NOTIMPL, other HRESULT failure, or mismatched readback is fatal
    for numeric settings. Denoise null explicitly leaves that option untouched
    and returns a note instead of claiming that denoise is disabled.
    """
    wanted = validate_imaging(settings)
    actual = {}

    options = (
        ('bit_depth', 'MIICAM_OPTION_BITDEPTH', 0, 8),
        ('binning', 'MIICAM_OPTION_BINNING', 1, 1),
    )
    for field, constant_name, sdk_value, profile_value in options:
        try:
            option = getattr(mii, constant_name)
        except AttributeError as exc:
            raise ImagingApplyError(field, 'SDK constant', constant_name) from exc
        _set_and_check(field,
                       lambda o=option, v=sdk_value: camera.put_Option(o, v),
                       lambda o=option: camera.get_Option(o), sdk_value)
        actual[field] = profile_value

    _call('roi', 'write', camera.put_Roi, 0, 0, 0, 0)
    roi = _call('roi', 'readback', camera.get_Roi)
    if roi != (0, 0, 0, 0):
        native_size = _call('roi', 'read native size', camera.get_Size)
        if roi != (0, 0, *native_size):
            raise ImagingApplyError('roi', 'readback',
                                    f'requested full ROI, got {roi!r} at native size {native_size!r}')
    actual['roi'] = 'full'

    for field, suffix in (
        ('gamma', 'Gamma'), ('contrast', 'Contrast'),
        ('brightness', 'Brightness'), ('hue', 'Hue'),
        ('saturation', 'Saturation'),
    ):
        actual[field] = _set_and_check(
            field, lambda s=suffix, f=field: getattr(camera, 'put_' + s)(wanted[f]),
            lambda s=suffix: getattr(camera, 'get_' + s)(), wanted[field])

    # The SDK packs threshold/radius/strength into one option. A valid radius
    # remains 2 even when strength 0 disables sharpening; only strength is in
    # the profile schema. Preserve threshold and radius for nonzero requests.
    try:
        sharpening_option = mii.MIICAM_OPTION_SHARPENING
    except AttributeError as exc:
        raise ImagingApplyError('sharpening', 'SDK constant', 'MIICAM_OPTION_SHARPENING') from exc
    packed = _call('sharpening', 'read current option', camera.get_Option, sharpening_option)
    if wanted['sharpening']:
        packed = (packed & 0xffff0000) | wanted['sharpening']
        if ((packed >> 16) & 0xff) == 0:
            packed |= 2 << 16
    else:
        packed = (packed & 0xffff0000) | (2 << 16)
    _call('sharpening', 'write', camera.put_Option, sharpening_option, packed)
    sharpen_readback = _call('sharpening', 'readback', camera.get_Option, sharpening_option)
    actual['sharpening'] = sharpen_readback & 0xffff
    if actual['sharpening'] != wanted['sharpening']:
        raise ImagingApplyError('sharpening', 'readback',
                                f"requested {wanted['sharpening']}, got {actual['sharpening']}")

    if wanted['denoise'] is None:
        actual['denoise'] = None
        actual['denoise_note'] = 'SDK denoise was not controlled or verified'
    else:
        try:
            denoise_option = mii.MIICAM_OPTION_DENOISE
        except AttributeError as exc:
            raise ImagingApplyError('denoise', 'SDK constant', 'MIICAM_OPTION_DENOISE') from exc
        actual['denoise'] = _set_and_check(
            'denoise', lambda: camera.put_Option(denoise_option, wanted['denoise']),
            lambda: camera.get_Option(denoise_option), wanted['denoise'])

    temperature = wanted['white_balance_temp']
    tint = wanted['white_balance_tint']
    white_balance = _set_and_check(
        'white_balance_temp/white_balance_tint',
        lambda: camera.put_TempTint(temperature, tint), camera.get_TempTint,
        (temperature, tint))
    actual['white_balance_temp'], actual['white_balance_tint'] = white_balance

    low = wanted['level_low']
    high = wanted['level_high']
    levels = _set_and_check('level_low/level_high',
                            lambda: camera.put_LevelRange(low, high),
                            camera.get_LevelRange, (tuple(low), tuple(high)))
    actual['level_low'] = list(levels[0])
    actual['level_high'] = list(levels[1])
    return actual
