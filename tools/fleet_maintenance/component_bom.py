"""Immutable source file-set BOM; installation and loaded bytes are independent."""
from collections.abc import Mapping, Sequence, Callable
from copy import deepcopy
from host.common.governance_contract import COMPONENT_IDS, ProbeResult, validate_probe_result
from host.common.strict_json import canonical_json, strict_json_loads
from .release_registry import (PIN, OVERLAY_ID, OVERLAY_PINS, digest, file_set_digest,
                               overlay_payload, valid_ref, valid_path)

def unknown():
    return {'sha256': None, 'status': 'unknown', 'evidence': None}

def build_bom(*, source_manifest: Mapping, read_blob: Callable[[str, str], bytes],
              overlays: Sequence[Mapping]) -> dict:
    """Read only caller-enumerated immutable Git blobs; never discover source files."""
    canonical_json(source_manifest)
    if (set(source_manifest) != {'schema_version', 'base_commit', 'components'}
            or type(source_manifest['schema_version']) is not int or source_manifest['schema_version'] != 1
            or not valid_ref(source_manifest['base_commit'])):
        raise ValueError('invalid source manifest')
    records = source_manifest['components']
    if (len(records) != 9 or {r['component_id'] for r in records} != set(COMPONENT_IDS)):
        raise ValueError('all nine unique components required')
    by_id = {}
    for record in records:
        if set(record) != {'component_id', 'files', 'approval'}:
            raise ValueError('invalid component record')
        files = []
        for f in record['files']:
            if (set(f) != {'ref', 'path', 'sha256', 'provenance', 'resource'}
                    or not valid_ref(f['ref']) or not valid_path(f['path'])
                    or not valid_path(f['resource']) or not isinstance(f['provenance'], str)
                    or not f['provenance'] or f['provenance'].startswith('/')):
                raise ValueError('explicit immutable ref/path/logical provenance required')
            data = read_blob(f['ref'], f['path'])
            if not isinstance(data, bytes) or digest(data) != f['sha256']:
                raise ValueError('source file hash mismatch: '+f['path'])
            files.append(dict(f))
        if len({f['path'] for f in files}) != len(files):
            raise ValueError('duplicate source path')
        by_id[record['component_id']] = {
            'component_id': record['component_id'],
            'source': {'ref': source_manifest['base_commit'] if files else None,
                       'sha256': file_set_digest(files) if files else None,
                       'provenance': sorted({f['provenance'] for f in files}),
                       'status': 'verified_files' if files else 'unknown', 'files': files},
            'installed': unknown(), 'runtime': unknown(), 'approval': deepcopy(record['approval'])}
    applied = []
    if len(overlays) > 1:
        raise ValueError('only original fixed overlay permitted')
    for overlay in overlays:
        if (set(overlay) != {'overlay_id', 'ref', 'resources', 'approval'} or overlay['overlay_id'] != OVERLAY_ID
                or not valid_ref(overlay['ref']) or source_manifest['base_commit'] != PIN
                or set(overlay['resources']) != set(OVERLAY_PINS) | {'stable_manifest.json'}):
            raise ValueError('unregistered overlay or baseline')
        resources = {}
        for name, path in overlay['resources'].items():
            if not valid_path(path):
                raise ValueError('invalid resource path')
            resources[name] = read_blob(overlay['ref'], path)
        manifest, payload = overlay_payload(resources)
        collector = by_id['collector']['source']; files = collector['files']
        baseline = strict_json_loads(resources['stable_manifest.json'].decode())
        if {f['path']: f['sha256'] for f in files} != {f['path']: f['sha256'] for f in baseline['files']}:
            raise ValueError('overlay requires exact 236-file f5 baseline')
        delta = {f['path']: f for f in manifest['files']}
        for f in files:
            if f['path'] in payload:
                if f['sha256'] != delta[f['path']]['before_sha256']:
                    raise ValueError('overlay before mismatch')
                f.update(ref=overlay['ref'], sha256=digest(payload[f['path']]),
                         provenance='registered:'+OVERLAY_ID, resource='payload/'+f['path'])
        collector.update(sha256=file_set_digest(files),
                         provenance=sorted({f['provenance'] for f in files}))
        applied.append(deepcopy(dict(overlay)))
    return {'schema_version': 1, 'base_commit': source_manifest['base_commit'],
            'overlays': applied, 'components': [by_id[c] for c in COMPONENT_IDS]}

def compare_components(bom: Mapping, observation: ProbeResult | None) -> dict:
    """Return a copy. A matching file is not whole-component or loaded-byte proof."""
    result = deepcopy(dict(bom))
    if observation is not None:
        observation = validate_probe_result(observation)
    fields = dict(observation.fields) if observation and observation.identity_verified else {}
    for component in result['components']:
        for category in ('installed', 'runtime'):
            component[category] = unknown()
            evidence = fields.get(category+'_components')
            if evidence is None or evidence.quality != 'observed':
                continue
            entry = strict_json_loads(evidence.value_json).get(component['component_id'])
            if not entry or entry['status'] != 'observed':
                continue
            # Probe protocol proves a file only when its exact file: label matches.
            prefix = 'file:' if category == 'installed' else 'loaded:file:'
            matched = next((f for f in component['source']['files']
                            if entry['source'] == prefix+f['path']), None)
            if matched is None:
                continue
            component[category] = {
                'sha256': entry['sha256'], 'status': 'observed',
                'evidence': {'source': entry['source'], 'scope': 'file',
                             'matches_source': entry['sha256'] == matched['sha256'],
                             'observed_at': evidence.observed_at}}
    return result
