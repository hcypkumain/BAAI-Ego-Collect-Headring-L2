"""Shared synthetic factories. Never use these as a production inventory."""
from dataclasses import asdict

from host.common.device_registry import Device, parse_registry
from host.common.governance_contract import (COMPONENT_IDS, FIELD_IDS, Evidence,
                                             ProbePolicy, ProbeResult)
from host.common.strict_json import canonical_json
from host.common.strict_json import require

DEFAULT_DEVICE_IDS = ('cp_0007', 'cp_0011', 'cp_0013', 'cp_0014', 'cp_0015', 'cp_0042')
LEGACY_UPLOAD_IDS = ('cp_0007', 'cp_0011', 'cp_0013', 'cp_0014')
SAMPLE_TIME = '2026-11-13T00:00:00Z'


def sample_grants():
    """Explicit synthetic migration grants, not inferred from enrollment."""
    return {did: [{'operation': 'upload', 'destination': destination,
                   'reason': 'legacy: synthetic migration scope', 'owner': 'fixture-owner',
                   'expiry': None} for destination in ('EGOL2', 'EGOL3_hand')]
            for did in LEGACY_UPLOAD_IDS}


def sample_device(device_id='cp_0042', *, index=1, **overrides):
    """Return a Device; index selects an RFC5737 address, not a real endpoint."""
    row = dict(device_id=device_id, ssh_alias='test-' + device_id,
               address='192.0.2.' + str(index), ssh_user='fixture',
               host_key_alias='test-key-' + device_id, capabilities=('probe', 'receive'),
               enrolled=True, receive_member=True, monitor_enabled=True, upload_grants=())
    row.update(overrides)
    return Device(**row)


def sample_registry(device_ids=DEFAULT_DEVICE_IDS, *, receive=None, monitor=None,
                    enrolled=None, grants=None):
    """None chooses fixture defaults; an explicit empty grants mapping denies all."""
    receive = set(device_ids if receive is None else receive)
    monitor = set(device_ids if monitor is None else monitor)
    enrolled = set(device_ids if enrolled is None else enrolled)
    grants = sample_grants() if grants is None else grants
    rows = []
    for index, did in enumerate(device_ids, 1):
        row = asdict(sample_device(did, index=index, receive_member=did in receive,
                                  monitor_enabled=did in monitor, enrolled=did in enrolled))
        row['capabilities'] = list(row['capabilities'])
        row['upload_grants'] = [asdict(g) if hasattr(g, '__dataclass_fields__') else dict(g)
                                for g in grants.get(did, ())]
        rows.append(row)
    return parse_registry({'schema_version': 1, 'devices': rows})


def sample_evidence(value=None, *, source='fixture:unknown', observed_at=None,
                    quality=None, reason=None):
    if quality is None:
        quality = 'unknown' if value is None else 'observed'
    if observed_at is None and value is not None:
        observed_at = SAMPLE_TIME
    return Evidence(None if value is None else canonical_json(value), source,
                    observed_at, quality, reason)


def sample_probe_result(device_id='cp_0042', *, registry_digest=None, generation=1,
                        identity_verified=True, status='partial', fields=None):
    if registry_digest is None:
        registry_digest = sample_registry().digest
    evidence = {name: sample_evidence() for name in FIELD_IDS}
    if fields is not None:
        require(set(fields) <= set(FIELD_IDS), 'Unknown fixture field override')
        evidence.update(fields)
    return ProbeResult(registry_digest, device_id, generation, identity_verified, status,
                       tuple((name, evidence[name]) for name in FIELD_IDS))


def sample_probe_policy(**overrides):
    row = dict(ssh_config='/synthetic/trust/ssh_config', known_hosts='/synthetic/trust/known_hosts',
               identity_path='/synthetic/device/identity.json',
               component_paths=tuple((name, '/synthetic/source/' + name + '.py')
                                     for name in COMPONENT_IDS), disk_mount=None)
    row.update(overrides)
    return ProbePolicy(**row)
