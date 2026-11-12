"""Pure, explicit v1 evidence. Never derive identities from legacy labels.

Schema validation is compiled at packaging/import time; the functions below do
no IO. contracts/v1 must accompany this source in an offline release bundle.
"""
import copy
import json
from pathlib import Path


def _validators():
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource
    root = next(p / 'contracts/v1/schemas' for p in Path(__file__).resolve().parents
                if (p / 'contracts/v1/schemas').is_dir())
    schemas = [json.loads(p.read_text()) for p in root.glob('*.json')]
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas)
    return {s['$id'].rsplit('/', 1)[-1]: Draft202012Validator(s, registry=registry, format_checker=FormatChecker()) for s in schemas}


VALIDATORS = _validators()


def loads(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def nonfinite(_): raise ValueError('nonfinite JSON')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def sample(source, boot_id=None, stale_after_ms=5000):
    return dict(source=source, boot_id=boot_id, sampled_mono_ms=None, age_ms=None,
                stale_after_ms=stale_after_ms, quality='unknown', reason='NOT_OBSERVED')


def unknown(device_id, boot_id=None, controller_instance=None):
    s = lambda source: sample(source, boot_id)
    context = dict.fromkeys(('session_id', 'task_group_id', 'ref_id', 'asset_sha256', 'list_id',
                            'list_revision', 'task_policy_id', 'task_policy_version', 'profile_id', 'profile_epoch'))
    role = dict.fromkeys(('binding_id', 'confirmed', 'calibration_valid', 'calibration_version', 'profile_epoch', 'proof_boot_id'))
    role['reason'] = 'NOT_OBSERVED'
    return dict(schema_version='1.0', device_id=device_id, boot_id=boot_id, controller_instance=controller_instance,
        seq=None, time=dict(utc=None, utc_trusted=None, monotonic_ms=None), sample=s('controller'),
        mode='unknown', lifecycle='unknown', recording_active=None, state_revision=None, session=context,
        progress=dict(sample=s('device_ledger'), **dict.fromkeys(('total', 'completed', 'pending', 'reserved', 'remaining', 'failed_attempts')), reason='POLICY_UNKNOWN'),
        resources=dict(disk_sample=sample('resource_sampler', boot_id, 90000), disk_volume_id=None,
                       disk_free_bytes=None, disk_available_bytes=None, ram_sample=sample('resource_sampler', boot_id, 15000), ram_mem_available_bytes=None),
        health=dict(sample=s('worker'), camera='unknown', eeg='unknown', log_dropped=None,
                    impedance=dict(measurement_id=None, owner_run=None, session_id=None, profile_epoch=None,
                                   quality_version=None, quality='unknown', sample=sample('worker', boot_id, 30000), can_review=None, reason='NOT_OBSERVED')),
        roles=dict(sample=s('role_registry'), left=copy.deepcopy(role), right=copy.deepcopy(role)),
        version=dict(sample=s('release_manifest'), base_commit=None, overlay_manifest_sha256=None,
                     installed_manifest_sha256=None, runtime_manifest_sha256=None, approved_release_id=None),
        usb_transport=dict(sample=s('phone_observer'), configured='unknown', current='unknown', state='unknown', reason='NOT_OBSERVED'),
        queues=dict(sample=s('device_ledger'), receipt_pending=None, handoff_pending=None, failed_attempts=None),
        recovery_state='unknown', data_filesystem_state='unknown')


def validate(value, kind='device'):
    VALIDATORS[kind + ('-snapshot' if kind == 'device' else '-fleet') + '-v1.schema.json'].validate(value)
    # Also reject nonfinite values before encoding and bool-as-int Python inputs.
    json.dumps(value, allow_nan=False)
    if kind == 'host':
        rows = value['devices']
        if len({r['device_id'] for r in rows}) != len(rows): raise ValueError('duplicate device')
        for row in rows:
            if row['snapshot'] is not None:
                if row['device_id'] != row['snapshot']['device_id']: raise ValueError('device mismatch')
                validate(row['snapshot'])
        return value
    def samples(obj):
        if not isinstance(obj, dict): return
        if 'sampled_mono_ms' in obj:
            if obj['quality'] == 'known':
                if (obj['boot_id'] is None or obj['boot_id'] != value['boot_id'] or
                    obj['age_ms'] is None or obj['sampled_mono_ms'] is None or
                    obj['age_ms'] > obj['stale_after_ms']): raise ValueError('sample identity/age')
                now = value['time']['monotonic_ms']
                if now is None or obj['sampled_mono_ms'] > now: raise ValueError('future sample')
            elif obj['reason'] is None: raise ValueError('unknown reason')
        for item in obj.values(): samples(item)
    samples(value)
    if value['sample']['quality'] == 'known' and (value['controller_instance'] is None or value['seq'] is None): raise ValueError('publish identity')
    p = value['progress']; counts = [p[k] for k in ('completed', 'pending', 'reserved', 'remaining')]
    if p['total'] is not None:
        if any(x is None for x in counts) or p['total'] != sum(counts): raise ValueError('exclusive counts')
        if any(value['session'][k] is None for k in ('list_id', 'list_revision', 'profile_epoch', 'task_policy_version')): raise ValueError('progress scope')
    # Independent accepted ledger counts survive an unknown plan/group policy.
    r = value['resources']
    if r['disk_free_bytes'] is not None and r['disk_available_bytes'] is not None and r['disk_available_bytes'] > r['disk_free_bytes']: raise ValueError('disk counts')
    for role in ('left', 'right'):
        r = value['roles'][role]
        if r['calibration_valid'] is True and (r['confirmed'] is not True or r['proof_boot_id'] != value['boot_id'] or r['profile_epoch'] != value['session']['profile_epoch']): raise ValueError('role proof')
    i = value['health']['impedance']
    if i['can_review'] is True:
        if i['quality'] == 'unknown' or any(i[k] is None for k in ('measurement_id', 'owner_run', 'quality_version')): raise ValueError('review evidence')
        if i['session_id'] is None or i['session_id'] != value['session']['session_id'] or i['profile_epoch'] != value['session']['profile_epoch']: raise ValueError('review identity')
    return value


def normalize(device_id, evidence=None, boot_id=None, controller_instance=None):
    result = unknown(device_id, boot_id, controller_instance)
    def merge(target, patch):
        for key, value in patch.items():
            if key not in target: raise ValueError('unexpected evidence field')
            if isinstance(target[key], dict) and isinstance(value, dict): merge(target[key], value)
            else: target[key] = copy.deepcopy(value)
    merge(result, evidence or {})
    for key, expected in (('device_id', device_id), ('boot_id', boot_id), ('controller_instance', controller_instance)):
        if expected is not None and result[key] != expected:
            raise ValueError('normalization identity mismatch')
    return validate(result)


def aged(value, elapsed_ms):
    """Observation stale never invalidates a completed measurement gate."""
    result = copy.deepcopy(value)
    def walk(obj):
        if not isinstance(obj, dict): return
        if 'sampled_mono_ms' in obj and obj['age_ms'] is not None:
            obj['age_ms'] += max(0, elapsed_ms)
            if obj['quality'] == 'known' and obj['age_ms'] > obj['stale_after_ms']:
                obj.update(quality='stale', reason='SAMPLE_EXPIRED')
        for item in obj.values(): walk(item)
    walk(result)
    return result
