#!/usr/bin/env python3
"""Generate reviewable installation/rollback PLANS, never apply or run services.

Existing file snapshots are optional and only read from an explicit fixture root.
This command does not establish idle state or authorize a production mode switch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'orangepi/src/orangepi_collect/common'))
from phone_transport import load_config

FILES = {
    'services/phone-tether/tether_link.py': '/opt/headring-phone-transport/tether_link.py',
    'services/phone-tether/transport_config.py': '/opt/headring-phone-transport/transport_config.py',
    'orangepi/src/orangepi_collect/common/phone_transport.py': '/opt/headring-phone-transport/phone_transport.py',
    'deployment/systemd/headring-phone-tether.service': '/etc/systemd/system/headring-phone-tether.service',
    'deployment/phone-tether/20-transport-mode.conf': '/etc/systemd/system/usb-phone-link.service.d/20-transport-mode.conf',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path or path == Path('/') or Path('/etc') == path or Path('/etc') in path.parents:
        raise ValueError('explicit symlink-free staging/fixture outside /etc required')
    return path


def existing(root, target):
    if root is None:
        return {'status': 'not_inspected', 'existed': None, 'mode': None, 'sha256': None}, None
    file = root / target.lstrip('/')
    if file.resolve() != file:
        raise ValueError('symlink in snapshot target')
    try:
        info = file.stat()
    except FileNotFoundError:
        return {'status': 'absent', 'existed': False, 'mode': None, 'sha256': None}, None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ValueError('unsafe snapshot')
    data = file.read_bytes()
    return {'status': 'snapshotted', 'existed': True, 'mode': stat.S_IMODE(info.st_mode), 'sha256': digest(data)}, data


def plan(staging, config_path, snapshot_root=None):
    staging = safe_directory(staging)
    root = safe_directory(snapshot_root) if snapshot_root else None
    config = load_config(config_path)
    content = {target: (ROOT / source).read_bytes() for source, target in FILES.items()}
    content['/etc/headring-phone-transport.json'] = (json.dumps(config, indent=2) + '\n').encode()
    entries = []
    backups = {}
    for index, (target, data) in enumerate(content.items()):
        before, old = existing(root, target)
        entry = {'target': target, 'payload': f'payload/{index}', 'mode': 0o640 if target.endswith('.json') else 0o644,
                 'owner': 'root', 'group': 'orangepi' if target.endswith('.json') else 'root',
                 'sha256': digest(data), 'before': before}
        if old is not None:
            entry['backup'] = f'backup/{index}'
            backups[entry['backup']] = old
        entries.append(entry)
    manifest = {'schema': 1, 'status': 'plan_only_not_installed', 'mode': config['mode'],
                'snapshot_root': str(root) if root else None, 'files': entries,
                'requires': ['operator-verified authoritative safe idle', 'explicit stop old mode before config change',
                             'refuse activation while other mode active', 'review route inventory and generic Ethernet auto-profile competition'],
                'network_profile_installed': False}
    serialized = json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    if staging.exists():
        if (staging / 'manifest.json').read_text() != serialized:
            raise ValueError('staging conflict; use a new directory')
        rollback_plan(staging)
        return manifest
    staging.mkdir(mode=0o700, parents=True)
    (staging / 'payload').mkdir(mode=0o700)
    (staging / 'backup').mkdir(mode=0o700)
    for entry, data in zip(entries, content.values()):
        (staging / entry['payload']).write_bytes(data)
        (staging / entry['payload']).chmod(entry['mode'])
    for name, data in backups.items():
        (staging / name).write_bytes(data)
        (staging / name).chmod(0o600)
    (staging / 'manifest.json').write_text(serialized)
    (staging / 'manifest.json').chmod(0o600)
    return manifest


def rollback_plan(staging):
    staging = safe_directory(staging)
    manifest_path = staging / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('manifest symlink')
    manifest = json.loads(manifest_path.read_text())
    allowed = set(FILES.values()) | {'/etc/headring-phone-transport.json'}
    if manifest['schema'] != 1 or manifest['status'] != 'plan_only_not_installed':
        raise ValueError('not an unapplied installation plan')
    root = safe_directory(manifest['snapshot_root']) if manifest['snapshot_root'] else None
    for index, entry in enumerate(manifest['files']):
        if entry['target'] not in allowed or entry['payload'] != f'payload/{index}':
            raise ValueError('unexpected plan path')
        payload = staging / entry['payload']
        if payload.resolve() != payload or digest(payload.read_bytes()) != entry['sha256']:
            raise ValueError('payload modified: rollback conflict')
        if existing(root, entry['target'])[0] != entry['before']:
            raise ValueError('target modified: rollback conflict')
        if 'backup' in entry:
            if entry['backup'] != f'backup/{index}':
                raise ValueError('unexpected backup path')
            backup = staging / entry['backup']
            if backup.resolve() != backup or digest(backup.read_bytes()) != entry['before']['sha256']:
                raise ValueError('backup modified: rollback conflict')
    return {'status': 'plan_only_nothing_installed', 'actions': [],
            'note': 'No rollback performed. Future installation needs its own applied manifest and post-install conflict checks.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'rollback-plan'))
    parser.add_argument('--staging', required=True)
    parser.add_argument('--config', default=str(ROOT / 'deployment/phone-tether/transport.example.json'))
    parser.add_argument('--snapshot-root', help='offline target filesystem fixture only')
    args = parser.parse_args()
    try:
        result = plan(args.staging, args.config, args.snapshot_root) if args.command == 'plan' else rollback_plan(args.staging)
        # Do not print config, serials, backup contents or credentials.
        print(json.dumps({'status': result['status'], 'file_count': len(result.get('files', []))}))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('Plan refused: invalid input or file conflict', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
