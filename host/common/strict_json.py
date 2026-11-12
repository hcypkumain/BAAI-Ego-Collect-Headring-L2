"""Strict, deterministic JSON and scalar validation, without I/O."""
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    require(type(value) is str and bool(value.strip()) and
            not any(ord(c) < 32 or ord(c) == 127 for c in value), 'Invalid text')
    return value


def exact_fields(value, names):
    require(isinstance(value, Mapping) and set(value) == set(names), 'Unexpected fields')


def utc_timestamp(value):
    require(type(value) is str and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value) is not None,
        'Expected UTC RFC3339 timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError('Invalid UTC timestamp') from exc


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite JSON number')


def _json_types(value):
    if type(value) is dict:
        require(all(type(k) is str for k in value), 'JSON keys must be strings')
        for item in value.values():
            _json_types(item)
    elif type(value) in (list, tuple):
        for item in value:
            _json_types(item)
    else:
        require(value is None or type(value) in (str, bool, int, float), 'Invalid JSON type')


def canonical_json(value):
    _json_types(value)
    # Reject Unicode surrogates as well as NaN/Infinity (including overflowing exponents).
    result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':'), allow_nan=False)
    result.encode('utf-8')
    return result


def strict_json_loads(value):
    require(type(value) is str, 'JSON input must be text')
    result = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
    canonical_json(result)
    return result
