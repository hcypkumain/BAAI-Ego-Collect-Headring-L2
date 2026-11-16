"""Validate local USB export tombstones without requiring the USB to be attached.

This is only the evidence reader; the cleanup transaction that writes these
records has not yet been integrated.
"""
import hashlib
import json
import re
from pathlib import Path


def evidence_path(base, source):
    key = hashlib.sha256(str(source).encode('utf-8')).hexdigest()
    return Path(base) / 'runtime' / 'usb_exports' / 'completed' / (key + '.json')


def successful_export(base, project, entry, image):
    if image is None or not isinstance(entry, dict) or entry.get('status') != 'success':
        return False
    try:
        source_value = entry.get('session')
        if not isinstance(source_value, str) or not Path(source_value).is_absolute():
            return False
        source = Path(source_value)
        if source.exists() or source.is_symlink():
            return False
        reference = Path(image).resolve().relative_to(Path(project).resolve()).as_posix()
        evidence = evidence_path(base, source_value)
        if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 16 * 1024 * 1024:
            return False
        data = json.loads(evidence.read_text(encoding='utf-8'))
        manifest = data['manifest']
        rows = manifest['files']
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return (data.get('version') == 1 and data.get('state') == 'complete'
                and data.get('source') == source_value and data.get('reference_path') == reference
                and data.get('qualified_before_export') is True and data.get('local_deleted') is True
                and data.get('verification') == 'sha256' and isinstance(rows, list) and bool(rows)
                and digest == manifest.get('digest')
                and all(isinstance(row, dict) and re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', ''))) for row in rows))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
