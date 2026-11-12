"""Explicit, offline legacy inventory conversion; never opens a default config."""
import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path

from host.common.device_registry import parse_registry
from host.common.strict_json import canonical_json, strict_json_loads


def migrate_legacy(legacy: Mapping, *, receive_members: Sequence, grants: Mapping) -> dict:
    """Require explicit trust identities, receive selection and per-root grants.

    Input devices use id (or device_id), ssh_alias, address, ssh_user,
    host_key_alias. Missing identity fields are an error, not SSH discovery.
    enabled defaults false; monitor_enabled defaults false. Receive membership
    and grants are independent inputs and never re-enable a disabled device.
    """
    if not isinstance(legacy, Mapping) or not isinstance(legacy.get('devices'), list):
        raise ValueError('Explicit legacy devices array required')
    if isinstance(receive_members, (str, bytes)) or not isinstance(receive_members, Sequence):
        raise ValueError('Explicit receive members required')
    if len(set(receive_members)) != len(receive_members) or not isinstance(grants, Mapping):
        raise ValueError('Duplicate receive members or invalid grants')
    rows = []
    changes = []
    for old in legacy['devices']:
        if not isinstance(old, Mapping):
            raise ValueError('Invalid legacy device')
        did = old.get('device_id', old.get('id'))
        if 'id' in old and 'device_id' in old and old['id'] != old['device_id']:
            raise ValueError('Ambiguous legacy device identity')
        enabled = old.get('enabled', False)
        monitor = old.get('monitor_enabled', False)
        if type(enabled) is not bool or type(monitor) is not bool:
            raise ValueError('Legacy enable flags must be explicit booleans')
        row = {key: old[key] for key in ('ssh_alias', 'address', 'ssh_user', 'host_key_alias')}
        row.update(device_id=did, capabilities=list(old.get('capabilities', ())),
                   enrolled=enabled, receive_member=did in receive_members,
                   monitor_enabled=monitor,
                   upload_grants=[asdict(g) if is_dataclass(g) else dict(g)
                                  for g in grants.get(did, ())])
        rows.append(row)
        changes.append(dict(device_id=did, enrolled=enabled,
                            receive_member=row['receive_member'], monitor_enabled=monitor,
                            upload_destinations=[g['destination'] for g in row['upload_grants']]))
    ids = {row['device_id'] for row in rows}
    if set(receive_members) - ids or set(grants) - ids:
        raise ValueError('Selection/grants reference an unknown legacy device')
    value = dict(schema_version=1, devices=rows)
    registry = parse_registry(value)
    return dict(registry=value, digest=registry.digest, diff=changes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('legacy', 'receive-members', 'grants'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = migrate_legacy(strict_json_loads(args.legacy.read_text()),
                            receive_members=strict_json_loads(args.receive_members.read_text()),
                            grants=strict_json_loads(args.grants.read_text()))
    print(canonical_json(result))


if __name__ == '__main__':
    main()
