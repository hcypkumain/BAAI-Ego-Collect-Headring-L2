"""Build a deterministic, explicit immutable host payload, never a system install."""
import argparse
import gzip
import hashlib
import importlib
import io
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.strict_json import canonical_json, require, strict_json_loads
from host.common.governance_contract import COMPONENT_IDS

_MANIFEST = 'deployment/manifests/governance-release.json'
_PROVENANCE = 'deployment/manifests/host-source-provenance.json'
# Code-owned allowlist, independent of user/manifest contents. No backups, inventory, trust or pipeline.
PAYLOAD_PATHS = (
    'host/__init__.py', 'host/common/__init__.py', 'host/common/strict_json.py',
    'host/common/device_registry.py', 'host/common/governance_contract.py',
    'host/qc/__init__.py', 'host/qc/fleet_monitor/__init__.py',
    'host/qc/fleet_monitor/governance_cli.py', 'host/qc/fleet_monitor/readonly_probe.py',
    'host/qc/fleet_monitor/isolated_transport.py', 'tools/__init__.py',
    'tools/fleet_maintenance/__init__.py', 'tools/fleet_maintenance/component_bom.py',
    'tools/fleet_maintenance/release_registry.py', 'contracts/v1/device-registry.schema.json',
    'configs/device-registry.example.json', _PROVENANCE, _MANIFEST,
    'docs/governance/CONTRACT.md', 'docs/governance/RELEASE_VALIDATION.md',
    'docs/governance/PROBE.md', 'docs/GOVERNANCE_RELEASE.md')


class BuildError(Exception):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (canonical_json(value) + '\n').encode('utf-8')


def _safe_name(name):
    require(type(name) is str and name and not name.startswith('/') and '\\' not in name and
            str(PurePosixPath(name)) == name and '..' not in PurePosixPath(name).parts,
            'Invalid archive member')


def _git_blob(root, ref, path):
    require(re.fullmatch('[0-9a-f]{40}', ref) is not None and path in PAYLOAD_PATHS,
            'Immutable explicit source required')
    metadata = subprocess.run(['git', '-C', str(root), 'ls-tree', ref, '--', path],
                              capture_output=True, check=True).stdout
    if not metadata:
        raise BuildError('ADAPTER_NOT_INTEGRATED' if path.endswith(
            ('readonly_probe.py', 'isolated_transport.py', 'component_bom.py', 'release_registry.py'))
                         else 'PAYLOAD_SOURCE_MISSING')
    require(metadata.split(b' ', 1)[0] in (b'100644', b'100755'), 'Git symlink/nonfile prohibited')
    data = subprocess.run(['git', '-C', str(root), 'show', ref + ':' + path],
                          capture_output=True, check=True).stdout
    require(len(data) <= 2097152, 'Payload member too large')
    return data


def _archive(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            _safe_name(name)
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, mtime=0, compresslevel=9) as output:
        output.write(raw.getvalue())
    return compressed.getvalue()


def _candidate_resources(files):
    resources = {'payload.tar.gz': files['payload.tar.gz'], 'BOM.json': files['BOM.json']}
    with tarfile.open(fileobj=io.BytesIO(files['payload.tar.gz']), mode='r:gz') as archive:
        members = archive.getmembers()
        require(len(members) == len(PAYLOAD_PATHS) + 1 and
                {member.name for member in members} == set(PAYLOAD_PATHS) | {'BOM.json'} and
                all(member.isfile() and member.size <= 2097152 for member in members),
                'Unexpected payload members')
        require(archive.extractfile('BOM.json').read() == files['BOM.json'], 'BOM payload mismatch')
        for name in PAYLOAD_PATHS:
            resources[name] = archive.extractfile(name).read()
    return resources


def verify_candidate(files, *, resource_digest):
    """Local artifact integrity only, not B's approval/release authorization validator."""
    require(set(files) == {'payload.tar.gz', 'BOM.json', 'release.json', 'router.json'},
            'Unexpected candidate artifacts')
    release = strict_json_loads(files['release.json'].decode())
    router = strict_json_loads(files['router.json'].decode())
    envelope = strict_json_loads(files['BOM.json'].decode())
    resources = _candidate_resources(files)
    digest = resource_digest(resources)
    for name in PAYLOAD_PATHS:
        require(_sha(resources[name]) == envelope['files'][name]['sha256'], 'Payload member digest mismatch')
    manifest = strict_json_loads(resources[_MANIFEST].decode())
    expected = {key: manifest[key] for key in
                ('release_id', 'overlay_ids', 'scope', 'reason', 'expiry', 'approval')}
    expected.update(base_commit=envelope['build_commit'], component_BOM=envelope['component_BOM'],
                    resource_digest=digest)
    require(type(release) is dict and set(release) == set(expected) and
            canonical_json(release) == canonical_json(expected) and
            router == {expected['release_id']: digest} and
            canonical_json(envelope['approval']) == canonical_json(manifest['approval']) and
            manifest['approval']['status'] == 'candidate', 'Candidate binding mismatch')
    return True


def _assemble(ref, blobs, build_bom, resource_digest):
    """Pure assembly from explicitly supplied immutable bytes; tests use synthetic bytes."""
    require(re.fullmatch('[0-9a-f]{40}', ref) is not None and
            set(blobs) == set(PAYLOAD_PATHS) and all(type(x) is bytes for x in blobs.values()),
            'Explicit complete payload required')
    manifest = strict_json_loads(blobs[_MANIFEST].decode())
    require(manifest['payload_paths'] == list(PAYLOAD_PATHS) and
            manifest['approval']['status'] == 'candidate' and manifest['scope'] == ['monitor'] and
            manifest['approval']['scope'] == ['monitor'] and
            manifest['reason'] == manifest['approval']['reason'] and
            manifest['expiry'] == manifest['approval']['expiry'],
            'Candidate manifest binding mismatch')
    # The frozen upstream provenance remains a separate selection record, not B's source schema.
    strict_json_loads(blobs[_PROVENANCE].decode())
    source_manifest = {'schema_version': 1, 'base_commit': ref, 'components': [
        {'component_id': name, 'files': [], 'approval': {
            'status': 'unknown', 'scope': [name], 'reason': 'not reviewed', 'expiry': None}}
        for name in COMPONENT_IDS]}
    monitor = next(row for row in source_manifest['components'] if row['component_id'] == 'monitor')
    monitor['files'] = [{'ref': ref, 'path': name, 'sha256': _sha(data),
                         'provenance': 'Git/host-governance/' + name, 'resource': name}
                        for name, data in sorted(blobs.items())]

    def read_blob(immutable_ref, path):
        require(immutable_ref == ref and path in blobs, 'Outside explicit immutable payload')
        return blobs[path]

    bom = build_bom(source_manifest=source_manifest, read_blob=read_blob, overlays=())
    require(type(bom) is dict, 'Invalid component BOM')
    envelope = {'schema_version': 1, 'build_commit': ref, 'component_BOM': bom,
                'runtime': {'implementation': platform.python_implementation(),
                            'python_version': platform.python_version(),
                            'openssh': 'unverified-not-invoked-by-builder'},
                'files': {name: {'sha256': _sha(data), 'bytes': len(data)}
                          for name, data in sorted(blobs.items())},
                'approval': manifest['approval'], 'production_pipeline_migrated': False}
    bom_bytes = _json_bytes(envelope)
    files = {**blobs, 'BOM.json': bom_bytes}
    payload = _archive(files)
    release = {key: manifest[key] for key in
               ('release_id', 'overlay_ids', 'scope', 'reason', 'expiry', 'approval')}
    resources = {**blobs, 'payload.tar.gz': payload, 'BOM.json': bom_bytes}
    digest = resource_digest(resources)  # B's exact canonical resource-map digest, not archive SHA.
    release.update(base_commit=ref, component_BOM=bom, resource_digest=digest)
    router = {manifest['release_id']: digest}  # Local candidate only; never production routing.
    files = {'payload.tar.gz': payload, 'BOM.json': bom_bytes,
             'release.json': _json_bytes(release), 'router.json': _json_bytes(router)}
    verify_candidate(files, resource_digest=resource_digest)
    return files


def _write_new(output, files):
    path = Path(output).absolute()
    require(path.name not in ('', '.', '..'), 'New output directory required')
    # All parents must already exist; no traversal through symlink ancestors, no overwrite.
    parts = path.parts
    require('..' not in parts, 'Output traversal prohibited')
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        os.mkdir(parts[-1], mode=0o700, dir_fd=fd)
        target = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            for name, data in files.items():
                require('/' not in name and name not in ('.', '..'), 'Output filename invalid')
                file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=target)
                with os.fdopen(file, 'wb') as stream:
                    stream.write(data)
        finally:
            os.close(target)
    finally:
        os.close(fd)


def build_release(*, root, ref, output):
    require(platform.python_implementation() == 'CPython' and platform.python_version() == '3.14.4',
            'Build runtime requires explicit revalidation')
    root = Path(root)
    resolved = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}'],
                              capture_output=True, check=True).stdout.decode().strip()
    require(re.fullmatch('[0-9a-f]{40}', resolved) is not None, 'Invalid immutable commit')
    blobs = {name: _git_blob(root, resolved, name) for name in PAYLOAD_PATHS}
    # Import B only after its selected Git bytes are bound to the executing implementation.
    module_path = 'tools/fleet_maintenance/component_bom.py'
    for name in PAYLOAD_PATHS:
        if name.endswith('.py'):
            path = root / name
            require(not any(p.is_symlink() for p in (path, *path.parents)) and
                    path.read_bytes() == blobs[name], 'Executing Python differs from immutable ref')
    try:
        module = importlib.import_module('tools.fleet_maintenance.component_bom')
    except ModuleNotFoundError as exc:
        raise BuildError('ADAPTER_NOT_INTEGRATED') from exc
    require(Path(module.__file__).resolve() == (root / module_path).resolve(), 'Wrong BOM module root')
    validator = importlib.import_module('tools.fleet_maintenance.release_registry')
    files = _assemble(resolved, blobs, module.build_bom, validator.resource_digest)
    resources = _candidate_resources(files)
    reasons = validator.validate_release(release=strict_json_loads(files['release.json'].decode()),
                                         router=strict_json_loads(files['router.json'].decode()),
                                         resources=resources)
    require(type(reasons) is tuple and all(type(code) is str and re.fullmatch('[a-z_]+', code)
                                         for code in reasons), 'Invalid release validation codes')
    _write_new(output, files)
    return {'build_commit': resolved, 'payload_sha256': _sha(files['payload.tar.gz']),
            'BOM_sha256': _sha(files['BOM.json']), 'approval': 'candidate',
            'release_blocking_codes': list(reasons),
            'host_runnable': False, 'live_read_verified': False,
            'note': 'Clean-package runtime and independent G2 validation still required'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New isolated directory, parent must exist')
    parser.add_argument('--ref', default='HEAD', help='Explicit Git commit/ref, resolved once')
    args = parser.parse_args(argv)
    try:
        result = build_release(root=Path(__file__).resolve().parents[1], ref=args.ref, output=args.output)
    except BuildError as exc:
        print(canonical_json({'error': str(exc)}), file=sys.stderr)
        return 3
    except Exception:
        print(canonical_json({'error': 'BUILD_REJECTED'}), file=sys.stderr)
        return 2
    print(canonical_json(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
