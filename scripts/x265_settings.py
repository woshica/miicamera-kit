"""Validated libx265 settings shared by profile loading and the writer."""

import math
import re


X265_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow", "placebo")

# Deliberately bounded to options used by the official-bitstream comparison.
# Rate control, GOP length, and B frame count are owned by Writer separately.
X265_PARAM_KEYS = frozenset((
    "frame-threads", "pools", "ref", "min-keyint", "b-adapt",
    "rc-lookahead", "scenecut", "ctu", "min-cu-size", "rect", "amp",
    "max-tu-size", "tu-inter-depth", "tu-intra-depth", "me", "subme",
    "merange", "weightp", "weightb", "sao", "rd", "aq-mode",
    "aq-strength", "cutree", "open-gop", "b-pyramid",
    "strong-intra-smoothing", "psy-rd", "deblock",
))
_INTEGER = re.compile(r"^[0-9]+$")
_DECIMAL = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_DEBLOCK = re.compile(r"^-?[0-9]+,-?[0-9]+$")
_FLOAT_KEYS = frozenset(("aq-strength", "psy-rd"))


def validate_x265_params(params):
    """Return a detached, JSON-serializable map safe for -x265-params."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("x265_params must be a dictionary")
    normalized = {}
    for key, value in params.items():
        if type(key) is not str or key not in X265_PARAM_KEYS:
            raise ValueError(f"Unsupported x265 parameter: {key!r}")
        if type(value) is bool or type(value) not in (int, float, str):
            raise ValueError(f"Invalid x265 value for {key}")
        if type(value) is float and not math.isfinite(value):
            raise ValueError(f"Invalid x265 value for {key}")
        pattern = (_DEBLOCK if key == "deblock" else
                   _DECIMAL if key in _FLOAT_KEYS else _INTEGER)
        if not pattern.fullmatch(str(value)):
            raise ValueError(f"Invalid x265 value for {key}")
        normalized[key] = value
    return normalized
