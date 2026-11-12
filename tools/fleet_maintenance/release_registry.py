"""Pure release routing and fixed overlay resource validation (no device I/O)."""
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import io
import re
import zipfile

from host.common.governance_contract import COMPONENT_IDS
from host.common.strict_json import canonical_json, strict_json_loads

PIN = 'f5d03b7b5fb197eb76e78610a0bce672e5cc4853'
STABLE_SHA = '95c02f40ad634a02218df4bc84c4093c3571cae3eace0dc6c95fa0e87f462109'
OVERLAY_ID = 'eeg-impedance-advisory-36d8059918ce'
OVERLAY_PINS = {
    'manifest.json': '36d8059918ce370f7928cc7cd3207e2ac1d26471b3e8dcdef74b98b7888cd62a',
    'bundle.zip': 'ee2eaf919287df1670fc15a8a706b648f13e24a8fc3a9119e748fef4677a759c',
    'transaction.py': 'fee73265c537bad5d777b21c5043657427b5503c6cd244e5df8d059a816ab057',
    'bootstrap.py': '455435313b5795f6bb476444a955a4b6a704947d9aa3a0b76e5a5865e9550ab5',
}

def digest(data):
    return hashlib.sha256(data).hexdigest()

def resource_digest(resources):
    return digest(canonical_json({k: digest(v) for k, v in resources.items()}).encode())

def file_set_digest(files):
    return digest(canonical_json(sorted(
        ({'path': f['path'], 'sha256': f['sha256']} for f in files),
        key=lambda f: f['path'])).encode())

def valid_ref(ref):
    return isinstance(ref, str) and re.fullmatch('[0-9a-f]{40}', ref) is not None

def valid_path(path):
    return (isinstance(path, str) and bool(path) and not path.startswith('/')
            and all(p not in ('', '.', '..') for p in path.split('/'))
            and not any(ord(c) < 32 or c == '\\' for c in path))

def overlay_payload(resources):
    """Reconstruct only the original five-file delta, checking every fixed pin."""
    if set(resources) != set(OVERLAY_PINS) | {'stable_manifest.json'}:
        raise ValueError('overlay resource set mismatch')
    if any(digest(resources[k]) != v for k, v in OVERLAY_PINS.items()):
        raise ValueError('overlay resource pin mismatch')
    if digest(resources['stable_manifest.json']) != STABLE_SHA:
        raise ValueError('stable manifest pin mismatch')
    stable = strict_json_loads(resources['stable_manifest.json'].decode())
    manifest = strict_json_loads(resources['manifest.json'].decode())
    before = {f['path']: f['sha256'] for f in stable['files']}
    entries = manifest['files']
    if (stable['commit'] != PIN or len(stable['files']) != 236 or len(before) != 236
            or manifest['commit'] != PIN or len(entries) != 5
            or len({f['path'] for f in entries}) != 5):
        raise ValueError('fixed baseline/count mismatch')
    with zipfile.ZipFile(io.BytesIO(resources['bundle.zip'])) as archive:
        expected = {'manifest.json', 'transaction.py'} | {'payload/'+f['path'] for f in entries}
        if len(archive.namelist()) != 7 or set(archive.namelist()) != expected:
            raise ValueError('fixed archive members mismatch')
        if any(archive.read(k) != resources[k] for k in ('manifest.json', 'transaction.py')):
            raise ValueError('archive resource mismatch')
        payload = {}
        for f in entries:
            data = archive.read('payload/'+f['path'])
            if (f['before_sha256'] != before.get(f['path']) or digest(data) != f['sha256']
                    or len(data) != f['size']):
                raise ValueError('before/after mismatch')
            payload[f['path']] = data
    return manifest, payload

def approved(value, scope):
    if not isinstance(value, Mapping) or set(value) != {'status', 'scope', 'reason', 'expiry'}:
        return False
    if (value['status'] != 'approved' or not isinstance(value['scope'], list)
            or value['scope'] != list(scope) or not scope
            or not isinstance(value['reason'], str) or not value['reason'].strip()):
        return False
    expiry = value['expiry']
    if expiry is None:
        return value['reason'].startswith('legacy:') and bool(value['reason'][7:].strip())
    try:
        if not isinstance(expiry, str) or not re.fullmatch(
                r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)', expiry):
            return False
        return datetime.fromisoformat(expiry.replace('Z', '+00:00')) > datetime.now(timezone.utc)
    except ValueError:
        return False

def _shape(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError('missing or extra evidence fields')

def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)

def _logical_label(value):
    return (_text(value) and not value.startswith('/') and '\\' not in value
            and all(p not in ('', '.', '..') for p in value.split('/')))

def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False

def _approval_shape(value):
    _shape(value, ('status', 'scope', 'reason', 'expiry'))
    if (value['status'] not in ('approved', 'unknown', 'rejected')
            or not isinstance(value['scope'], list)
            or any(not isinstance(c, str) or c not in COMPONENT_IDS for c in value['scope'])
            or len(set(value['scope'])) != len(value['scope']) or not _text(value['reason'])
            or (value['expiry'] is not None and not _utc(value['expiry']))):
        raise ValueError('invalid approval shape')

def _observation_shape(value, *, category, files):
    _shape(value, ('sha256', 'status', 'evidence'))
    if value['status'] == 'unknown':
        if value['sha256'] is not None or value['evidence'] is not None:
            raise ValueError('unknown observation has no bytes/evidence')
        return
    if value['status'] != 'observed' or not _hash(value['sha256']):
        raise ValueError('invalid observation status/hash')
    evidence = value['evidence']
    _shape(evidence, ('source', 'scope', 'matches_source', 'observed_at'))
    prefix = 'file:' if category == 'installed' else 'loaded:file:'
    if (not isinstance(evidence['source'], str) or not evidence['source'].startswith(prefix)
            or not valid_path(evidence['source'][len(prefix):]) or evidence['scope'] != 'file'
            or type(evidence['matches_source']) is not bool or not _utc(evidence['observed_at'])):
        raise ValueError('invalid same-file observation evidence')
    matching = next((f for f in files if prefix+f['path'] == evidence['source']), None)
    if matching is None or evidence['matches_source'] != (matching['sha256'] == value['sha256']):
        raise ValueError('observation is not matching same-file evidence')

def _bom_shape(bom):
    """Full local output contract, including out-of-scope unknown evidence."""
    _shape(bom, ('schema_version', 'base_commit', 'overlays', 'components'))
    if type(bom['schema_version']) is not int or bom['schema_version'] != 1 or not valid_ref(bom['base_commit']):
        raise ValueError('invalid BOM version/base')
    if not isinstance(bom['overlays'], list) or len(bom['overlays']) > 1:
        raise ValueError('invalid overlay list')
    for overlay in bom['overlays']:
        _shape(overlay, ('overlay_id', 'ref', 'resources', 'approval'))
        if not _logical_label(overlay['overlay_id']) or not valid_ref(overlay['ref']):
            raise ValueError('invalid overlay identity')
        _shape(overlay['resources'], set(OVERLAY_PINS) | {'stable_manifest.json'})
        if not all(valid_path(p) for p in overlay['resources'].values()):
            raise ValueError('invalid overlay resource path')
        _approval_shape(overlay['approval'])
    if not isinstance(bom['components'], list) or len(bom['components']) != 9:
        raise ValueError('nine components required')
    ids = []
    for component in bom['components']:
        _shape(component, ('component_id', 'source', 'installed', 'runtime', 'approval'))
        if not isinstance(component['component_id'], str) or component['component_id'] not in COMPONENT_IDS:
            raise ValueError('invalid component ID')
        ids.append(component['component_id'])
        source = component['source']
        _shape(source, ('ref', 'sha256', 'provenance', 'status', 'files'))
        if not isinstance(source['files'], list) or not isinstance(source['provenance'], list):
            raise ValueError('invalid source collections')
        files = source['files']
        for f in files:
            _shape(f, ('ref', 'path', 'sha256', 'provenance', 'resource'))
            if (not valid_ref(f['ref']) or not valid_path(f['path']) or not _hash(f['sha256'])
                    or not valid_path(f['resource']) or not _logical_label(f['provenance'])):
                raise ValueError('invalid source file evidence')
        if not files:
            if (source['status'] != 'unknown' or source['ref'] is not None
                    or source['sha256'] is not None or source['provenance'] != []):
                raise ValueError('unknown source must remain null')
        elif (source['status'] != 'verified_files' or not valid_ref(source['ref'])
              or source['ref'] != bom['base_commit'] or not _hash(source['sha256'])
              or source['sha256'] != file_set_digest(files)
              or len({f['path'] for f in files}) != len(files)
              or not all(_logical_label(p) for p in source['provenance'])
              or source['provenance'] != sorted({f['provenance'] for f in files})):
            raise ValueError('invalid verified file-set evidence')
        _approval_shape(component['approval'])
        for category in ('installed', 'runtime'):
            _observation_shape(component[category], category=category, files=files)
    if set(ids) != set(COMPONENT_IDS) or len(set(ids)) != 9:
        raise ValueError('duplicate/missing components')

def validate_release(*, release: Mapping, router: Mapping, resources: Mapping[str, bytes]) -> tuple[str, ...]:
    """Empty tuple alone means eligible; malformed/unknown evidence fails closed."""
    blocked = []
    def block(code):
        if code not in blocked:
            blocked.append(code)
    try:
        canonical_json(release)
        canonical_json(router)
        if set(release) != {'release_id', 'base_commit', 'overlay_ids', 'component_BOM',
                            'scope', 'reason', 'expiry', 'approval', 'resource_digest'}:
            block('release_schema')
        if not isinstance(release['release_id'], str) or not release['release_id']:
            block('release_identity')
        if not resources or any(not valid_path(k) or not isinstance(v, bytes) for k,v in resources.items()):
            block('resources_invalid')
        actual = resource_digest(resources)
        if release['resource_digest'] != actual:
            block('resource_digest_mismatch')
        if router.get(release['release_id']) != actual:
            block('router_resource_mismatch')
        scope = release['scope']
        if (not isinstance(scope, list) or not scope or len(set(scope)) != len(scope)
                or set(scope) - set(COMPONENT_IDS)):
            block('scope_invalid')
        approval = release['approval']
        _approval_shape(approval)
        if (not approved(approval, scope) or release['reason'] != approval.get('reason')
                or release['expiry'] != approval.get('expiry')):
            block('release_approval')
        bom = release['component_BOM']
        try:
            _bom_shape(bom)
        except (ValueError, TypeError, KeyError):
            block('bom_schema')
        if (type(bom['schema_version']) is not int or bom['schema_version'] != 1 or not valid_ref(release['base_commit'])
                or bom['base_commit'] != release['base_commit']):
            block('base_unknown')
        overlays = release['overlay_ids']
        if not isinstance(overlays, list) or overlays not in ([], [OVERLAY_ID]):
            block('overlay_unregistered')
        if overlays and 'collector' not in scope:
            block('overlay_scope')
        if [o['overlay_id'] for o in bom['overlays']] != overlays:
            block('overlay_bom_mismatch')
        if any(not approved(o['approval'], ['collector']) for o in bom['overlays']):
            block('overlay_approval')
        components = bom['components']
        if (len(components) != 9 or len({c['component_id'] for c in components}) != 9
                or {c['component_id'] for c in components} != set(COMPONENT_IDS)):
            block('component_coverage')
        for c in components:
            if c['component_id'] not in scope:
                continue
            if not approved(c['approval'], [c['component_id']]):
                block('component_approval_scope')
            source = c['source']; files = source['files']
            if (not files or source['status'] != 'verified_files'
                    or source['sha256'] != file_set_digest(files)
                    or len({f['path'] for f in files}) != len(files)):
                block('source_unknown')
            for f in files:
                if (not valid_path(f['path']) or not valid_ref(f['ref'])
                        or not f['provenance'] or digest(resources[f['resource']]) != f['sha256']):
                    block('source_resource_mismatch')
        if 'collector' in scope:
            if release['base_commit'] != PIN:
                block('collector_baseline_unknown')
            stable = strict_json_loads(resources['stable_manifest.json'].decode())
            if digest(resources['stable_manifest.json']) != STABLE_SHA or len(stable['files']) != 236:
                block('collector_manifest_pin')
            expected = {f['path']: f['sha256'] for f in stable['files']}
            if overlays == [OVERLAY_ID]:
                manifest, payload = overlay_payload({k:resources[k] for k in set(OVERLAY_PINS)|{'stable_manifest.json'}})
                expected.update({p: digest(data) for p,data in payload.items()})
            collector = next(c for c in components if c['component_id'] == 'collector')
            if {f['path']: f['sha256'] for f in collector['source']['files']} != expected:
                block('collector_reconstruction')
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration, zipfile.BadZipFile):
        block('malformed_or_missing_resource')
    return tuple(blocked)
