"""v1 only. No legacy SSH/status fallback and no database constructors."""
from snapshot_contract import loads, validate, aged


def device_snapshot(raw, expected_device):
    if not isinstance(raw, (bytes, str)) or len(raw if isinstance(raw, bytes) else raw.encode()) > 65536: raise ValueError('snapshot size')
    value = loads(raw)
    validate(value)
    if value['device_id'] != expected_device: raise ValueError('device identity')
    if any(value[k] is None for k in ('boot_id', 'controller_instance', 'seq')):
        raise ValueError('missing observation identity')
    return value


def restore_stale(value):
    """Cached evidence has no current-process monotonic age."""
    value = aged(value, 0)
    def walk(obj):
        if not isinstance(obj, dict): return
        if 'sampled_mono_ms' in obj:
            obj.update(age_ms=None)
            if obj['quality'] == 'known': obj.update(quality='stale', reason='HOST_RESTART')
        for item in obj.values(): walk(item)
    walk(value)
    return value
