"""Documentation-only offline schema/fixture checks; never import runtime code.

Cases use recursive object overlay (null is a value, arrays replace), optionally
replace synthetic UUID values before overlay. --show NAME emits the full wire
example to stdout, including negative examples. No file/network writes.
"""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def load(path):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key: " + key)
            out[key] = value
        return out
    return json.loads(path.read_text(), object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def overlay(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = overlay(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def replace(value, mapping):
    if isinstance(value, dict):
        return {k: replace(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [replace(v, mapping) for v in value]
    return mapping.get(value, value) if isinstance(value, str) else value


def semantic(kind, value):
    """Selected normative invariants, not a runtime state-machine test suite."""
    if kind == 'device':
        def samples(obj):
            if isinstance(obj, dict):
                if 'sampled_mono_ms' in obj:
                    if obj['quality'] == 'known':
                        assert obj['boot_id'] == value['boot_id'] and value['boot_id'] is not None
                        assert obj['age_ms'] is not None and obj['sampled_mono_ms'] is not None
                        assert obj['age_ms'] <= obj['stale_after_ms']
                        assert value['time']['monotonic_ms'] is not None
                        assert obj['sampled_mono_ms'] <= value['time']['monotonic_ms']
                    if obj['quality'] != 'known':
                        assert obj['reason'] is not None
                    if (obj['boot_id'] is not None and obj['boot_id'] == value['boot_id'] and
                        value['time']['monotonic_ms'] is not None and obj['sampled_mono_ms'] is not None and obj['age_ms'] is not None):
                        assert obj['age_ms'] >= value['time']['monotonic_ms'] - obj['sampled_mono_ms']
                for item in obj.values():
                    samples(item)
        samples(value)
        if value['sample']['quality'] == 'known':
            assert value['controller_instance'] is not None and value['seq'] is not None
        p = value['progress']
        counts = [p[k] for k in ('completed', 'pending', 'reserved', 'remaining')]
        if p['total'] is not None:
            assert all(v is not None for v in counts) and p['total'] == sum(counts)
        if p['total'] is not None:
            assert all(value['session'][k] is not None for k in ('list_id', 'list_revision', 'profile_epoch', 'task_policy_version'))
        r = value['resources']
        if r['disk_free_bytes'] is not None and r['disk_available_bytes'] is not None:
            assert r['disk_available_bytes'] <= r['disk_free_bytes']
        for role in ('left', 'right'):
            r = value['roles'][role]
            if r['calibration_valid']:
                assert value['boot_id'] is not None and value['session']['profile_epoch'] is not None
                assert all(r[k] is not None for k in ('binding_id', 'calibration_version', 'profile_epoch', 'proof_boot_id'))
                assert r['confirmed'] is True and r['proof_boot_id'] == value['boot_id']
                assert r['profile_epoch'] == value['session']['profile_epoch']
        i = value['health']['impedance']
        if i['can_review']:
            assert value['boot_id'] is not None and value['controller_instance'] is not None
            assert i['session_id'] is not None and i['profile_epoch'] is not None
            assert i['quality'] != 'unknown'
            assert all(i[k] is not None for k in ('measurement_id', 'owner_run', 'quality_version'))
            assert i['session_id'] == value['session']['session_id']
            assert i['profile_epoch'] == value['session']['profile_epoch']
    elif kind == 'command':
        p = value['proof']
        if p:
            assert all(p[k] == value[k] for k in ('boot_id', 'controller_instance', 'session_id'))
        if value['state'] == 'succeeded':
            assert value['effect'] == 'verified' and p is not None
            expected = {'START': 'capture_started', 'STOP': 'capture_stopped', 'CANCEL': 'never_started'}
            assert p['kind'] == expected[value['command']]
            if value['command'] == 'STOP':
                assert p['privacy_stop_confirmed'] is True
    elif kind == 'host':
        assert len({x['device_id'] for x in value['devices']}) == len(value['devices'])
        for row in value['devices']:
            if row['snapshot']:
                assert row['snapshot']['device_id'] == row['device_id']
                semantic('device', row['snapshot'])


def main():
    # Always parse every JSON with stdlib, even when jsonschema isn't installed.
    documents = {p: load(p) for p in ROOT.rglob('*.json')}
    bases = {k: load(ROOT / 'fixtures' / (k + '.json')) for k in ('device', 'host', 'command')}
    cases = load(ROOT / 'fixtures/cases.json')
    materialized = []
    for case in cases:
        value = overlay(replace(bases[case['base']], case.get('replace_values', {})), case['patch'])
        if case.get('attach_device'):
            value['devices'][0].update(snapshot=copy.deepcopy(bases['device']), identity_verified=True,
                                       connectivity='reachable', last_seen_utc='2026-11-12T12:00:00Z',
                                       last_seen_age_ms=0, reason=None)
        materialized.append((case, value))
    if len(sys.argv) == 3 and sys.argv[1] == '--show':
        print(json.dumps(next(v for c, v in materialized if c['name'] == sys.argv[2]), ensure_ascii=False, indent=2))
        return
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ImportError:
        print('JSON syntax OK:', len(documents), 'files; jsonschema unavailable, schema validation NOT RUN')
        return
    schemas = [v for p, v in documents.items() if p.parent.name == 'schemas']
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas)
    names = {'device': 'device-snapshot', 'host': 'host-fleet', 'command': 'command-receipt'}
    validators = {}
    for s in schemas:
        Draft202012Validator.check_schema(s)
    for kind, name in names.items():
        s = load(ROOT / 'schemas' / (name + '-v1.schema.json'))
        validators[kind] = Draft202012Validator(s, registry=registry, format_checker=FormatChecker())
    for case, value in materialized:
        valid = not list(validators[case['base']].iter_errors(value))
        if valid:
            try:
                semantic(case['base'], value)
            except (AssertionError, TypeError):
                valid = False
        assert valid == case['valid'], 'unexpected fixture result: ' + case['name']
    print('JSON syntax OK:', len(documents), 'files; schemas:', len(schemas), '; fixture expectations:', len(cases), 'PASS (offline)')


if __name__ == '__main__':
    main()
