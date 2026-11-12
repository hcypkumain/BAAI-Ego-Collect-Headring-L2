"""Immutable local probe evidence, not a new device snapshot protocol."""
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .device_registry import DEVICE_ID
from .strict_json import (canonical_json, exact_fields, require, strict_json_loads,
                          text, utc_timestamp)

COMPONENT_IDS = ('collector', 'phone_link', 'phone_media', 'helper', 'systemd_udev',
                 'host_qc', 'monitor', 'standardizer', 'uploader')
FIELD_IDS = ('boot_id', 'ram_available_bytes', 'disk_available_bytes',
             'controller_state', 'installed_components', 'runtime_components')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STATUSES = ('ok', 'partial', 'unavailable', 'identity_rejected', 'timeout', 'quarantined')


def _hash(value):
    require(type(value) is str and _HASH.fullmatch(value) is not None, 'Invalid SHA256')


def _path(value):
    text(value)
    require(value.startswith('/') and not value.startswith('//') and '\\' not in value and
            str(PurePosixPath(value)) == value and '..' not in PurePosixPath(value).parts,
            'Expected canonical absolute POSIX path')
    return value


@dataclass(frozen=True)
class Evidence:
    value_json: str | None
    source: str
    observed_at: str | None
    quality: str
    reason: str | None

    def __post_init__(self):
        text(self.source)
        require(type(self.quality) is str and self.quality in ('observed', 'unknown', 'stale', 'error'),
                'Invalid evidence quality')
        if self.reason is not None:
            text(self.reason)
        if self.observed_at is not None:
            utc_timestamp(self.observed_at)
        if self.value_json is not None:
            value = strict_json_loads(self.value_json)
            require(value is not None and canonical_json(value) == self.value_json,
                    'Evidence JSON must be canonical; unknown uses None')
        if self.quality in ('unknown', 'error'):
            require(self.value_json is None, 'Unknown/error cannot assert a value')
        else:
            require(self.value_json is not None and self.observed_at is not None,
                    'Observed/stale evidence requires value and sample timestamp')


def _field_value(name, evidence):
    if evidence.value_json is None:
        return
    value = strict_json_loads(evidence.value_json)
    if name in ('boot_id', 'controller_state'):
        text(value)
    elif name in ('ram_available_bytes', 'disk_available_bytes'):
        require(type(value) is int and value >= 0, 'Expected nonnegative integer evidence')
    else:
        require(type(value) is dict and set(value) <= set(COMPONENT_IDS), 'Invalid components')
        for row in value.values():
            exact_fields(row, ('sha256', 'status', 'source'))
            text(row['source'])
            require(type(row['status']) is str and row['status'] in ('observed', 'unknown'),
                    'Invalid component status')
            if row['status'] == 'unknown':
                require(row['sha256'] is None, 'Unknown component hash must be null')
            else:
                _hash(row['sha256'])


@dataclass(frozen=True)
class ProbeResult:
    registry_digest: str
    device_id: str
    generation: int
    identity_verified: bool
    status: str
    fields: tuple[tuple[str, Evidence], ...]

    def __post_init__(self):
        _hash(self.registry_digest)
        require(type(self.device_id) is str and DEVICE_ID.fullmatch(self.device_id) is not None,
                'Invalid device ID')
        require(type(self.generation) is int and self.generation >= 0, 'Invalid generation')
        require(type(self.identity_verified) is bool, 'Invalid identity flag')
        require(type(self.status) is str and self.status in _STATUSES, 'Invalid result status')
        require(type(self.fields) is tuple and len(self.fields) == len(FIELD_IDS), 'Invalid fields')
        names = []
        for item in self.fields:
            require(type(item) is tuple and len(item) == 2 and type(item[0]) is str and
                    item[0] in FIELD_IDS and type(item[1]) is Evidence, 'Invalid field pair')
            name, evidence = item
            names.append(name)
            _field_value(name, evidence)
            if not self.identity_verified:
                require(evidence.value_json is None and evidence.quality in ('unknown', 'error'),
                        'Unverified identity cannot provide observed facts')
        require(len(set(names)) == len(FIELD_IDS), 'Missing/duplicate field')
        if self.status == 'identity_rejected':
            require(not self.identity_verified, 'Rejected identity cannot be verified')
        if self.status == 'ok':
            require(self.identity_verified, 'OK requires verified identity')


@dataclass(frozen=True)
class ProbePolicy:
    ssh_config: str
    known_hosts: str
    identity_path: str
    component_paths: tuple[tuple[str, str], ...]
    disk_mount: str | None
    timeout_ms: int = 5000
    maximum_bytes: int = 1048576

    def __post_init__(self):
        for path in (self.ssh_config, self.known_hosts, self.identity_path):
            _path(path)
        require(type(self.component_paths) is tuple, 'Component paths must be immutable')
        names, paths = [], []
        for pair in self.component_paths:
            require(type(pair) is tuple and len(pair) == 2 and type(pair[0]) is str and
                    pair[0] in COMPONENT_IDS, 'Invalid policy component')
            names.append(pair[0])
            paths.append(_path(pair[1]))
        require(len(set(names)) == len(names) and len(set(paths)) == len(paths),
                'Duplicate policy component/path')
        if self.disk_mount is not None:
            _path(self.disk_mount)
        require(type(self.timeout_ms) is int and 0 < self.timeout_ms <= 5000, 'Invalid timeout')
        require(type(self.maximum_bytes) is int and 0 < self.maximum_bytes <= 1048576,
                'Invalid output limit')


def validate_evidence(value: Evidence | Mapping[str, object]) -> Evidence:
    if type(value) is Evidence:
        value.__post_init__()
        return value
    exact_fields(value, Evidence.__dataclass_fields__)
    return Evidence(**value)


def validate_probe_result(value: ProbeResult | Mapping[str, object]) -> ProbeResult:
    if type(value) is ProbeResult:
        value.__post_init__()
        return value
    exact_fields(value, ProbeResult.__dataclass_fields__)
    require(type(value['fields']) is list, 'Expected serialized fields array')
    fields = []
    for pair in value['fields']:
        require(type(pair) is list and len(pair) == 2, 'Invalid serialized field pair')
        fields.append((pair[0], validate_evidence(pair[1])))
    return ProbeResult(**{**value, 'fields': tuple(fields)})


def validate_probe_policy(value: ProbePolicy | Mapping[str, object]) -> ProbePolicy:
    if type(value) is ProbePolicy:
        value.__post_init__()
        return value
    # Defaults may be omitted only for the two declared dataclass defaults.
    require(isinstance(value, Mapping), 'Expected policy mapping')
    required = set(ProbePolicy.__dataclass_fields__) - {'timeout_ms', 'maximum_bytes'}
    require(required <= set(value) <= set(ProbePolicy.__dataclass_fields__), 'Unexpected policy fields')
    require(type(value['component_paths']) is list, 'Expected component paths array')
    pairs = []
    for pair in value['component_paths']:
        require(type(pair) is list and len(pair) == 2, 'Invalid serialized component pair')
        pairs.append(tuple(pair))
    return ProbePolicy(**{**value, 'component_paths': tuple(pairs)})
