"""Fail-closed controller gate for persistent USB export transactions.

A transaction is stored in each profile's runtime/usb_exports/pending.json.
Only a completed transaction may cease to block acquisition. Invalid or
unreadable state is treated as pending, not as permission to record.
"""
import json
from pathlib import Path
from collection_profiles import MODES, profile_base, selection


def pending_exports(code_base):
    base = Path(code_base)
    try:
        profile = selection(base)
        paths = [profile_base(base, profile, mode) / 'runtime/usb_exports/pending.json'
                 for mode in MODES]
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return [{'path': str(base), 'error': str(error)}]
    found = []
    for path in dict.fromkeys(paths):
        try:
            if path.is_symlink():
                raise ValueError('导出恢复记录不能是符号链接')
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError('导出恢复记录无效')
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('state'), str):
                raise ValueError('导出恢复记录格式无效')
            if value['state'] != 'complete':
                found.append({'path': str(path), 'state': value['state']})
        except (OSError, ValueError, TypeError, RecursionError) as error:
            found.append({'path': str(path), 'error': str(error)})
    return found
