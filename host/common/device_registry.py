"""Immutable enrollment and independent upload grants. No configuration or I/O."""
import hashlib
import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .strict_json import canonical_json, exact_fields, require, text, utc_timestamp

DEVICE_ID = re.compile(r'cp_[0-9]{4}\Z')
_ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
UPLOAD_DESTINATIONS = frozenset(('EGOL2', 'EGOL3_hand'))
LEGACY_REASON_PREFIX = 'legacy:'


def _identity(value):
    require(type(value) is str and DEVICE_ID.fullmatch(value) is not None, 'Invalid device ID')


def _alias(value):
    require(type(value) is str and _ALIAS.fullmatch(value) is not None, 'Invalid SSH identifier')


@dataclass(frozen=True)
class UploadGrant:
    operation: str
    destination: str
    reason: str
    owner: str
    expiry: str | None

    def __post_init__(self):
        require(type(self.operation) is str and self.operation == 'upload', 'Unsupported grant operation')
        require(type(self.destination) is str and self.destination in UPLOAD_DESTINATIONS,
                'Unsupported exact project destination')
        text(self.reason)
        text(self.owner)
        if self.expiry is None:
            require(self.reason.startswith(LEGACY_REASON_PREFIX) and
                    bool(self.reason[len(LEGACY_REASON_PREFIX):].strip()),
                    'Unbounded grant requires explicit legacy: reason')
        else:
            utc_timestamp(self.expiry)


@dataclass(frozen=True)
class Device:
    device_id: str
    ssh_alias: str
    address: str
    ssh_user: str
    host_key_alias: str
    capabilities: tuple[str, ...]
    enrolled: bool
    receive_member: bool
    monitor_enabled: bool
    upload_grants: tuple[UploadGrant, ...]

    def __post_init__(self):
        _identity(self.device_id)
        for value in (self.ssh_alias, self.ssh_user, self.host_key_alias):
            _alias(value)
        require(type(self.address) is str and '%' not in self.address, 'Explicit IP required')
        try:
            ipaddress.ip_address(self.address)
        except ValueError as exc:
            raise ValueError('Explicit IP required') from exc
        require(type(self.capabilities) is tuple, 'Capabilities must be immutable')
        for capability in self.capabilities:
            _alias(capability)
        require(len(set(self.capabilities)) == len(self.capabilities), 'Duplicate capability')
        require(all(type(v) is bool for v in
                    (self.enrolled, self.receive_member, self.monitor_enabled)), 'Invalid bool')
        require(type(self.upload_grants) is tuple and
                all(type(g) is UploadGrant for g in self.upload_grants), 'Invalid grants')
        scopes = [(g.operation, g.destination) for g in self.upload_grants]
        require(len(set(scopes)) == len(scopes), 'Duplicate grant scope')


def _validate_devices(devices):
    require(type(devices) is tuple and len(devices) <= 64 and
            all(type(d) is Device for d in devices), 'Invalid device collection')
    for attr in ('device_id', 'ssh_alias', 'host_key_alias'):
        values = [getattr(d, attr) for d in devices]
        require(len(set(values)) == len(values), 'Duplicate device identity')
    addresses = []
    # IPv4-mapped IPv6 must not evade the single-address identity check.
    for device in devices:
        address = ipaddress.ip_address(device.address)
        addresses.append(getattr(address, 'ipv4_mapped', None) or address)
    require(len(set(addresses)) == len(addresses), 'Ambiguous address identity')
    owners = {}
    for device in devices:
        for name in (device.device_id, device.ssh_alias, device.host_key_alias, device.address):
            try:
                address = ipaddress.ip_address(name)
                name = str(getattr(address, 'ipv4_mapped', None) or address)
            except ValueError:
                pass
            require(name not in owners or owners[name] == device.device_id,
                    'Cross-namespace identity ambiguity')
            owners[name] = device.device_id


def _digest(devices):
    value = {'schema_version': 1, 'devices': [asdict(d) for d in devices]}
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Registry:
    schema_version: int
    digest: str
    devices: tuple[Device, ...]

    def __post_init__(self):
        require(type(self.schema_version) is int and self.schema_version == 1, 'Invalid version')
        _validate_devices(self.devices)
        require(type(self.digest) is str and self.digest == _digest(self.devices), 'Digest mismatch')


def parse_registry(value: Mapping[str, object]) -> Registry:
    exact_fields(value, ('schema_version', 'devices'))
    require(type(value['schema_version']) is int and value['schema_version'] == 1, 'Invalid version')
    require(type(value['devices']) is list and len(value['devices']) <= 64, 'Invalid devices array')
    devices = []
    for row in value['devices']:
        exact_fields(row, Device.__dataclass_fields__)
        require(type(row['capabilities']) is list and type(row['upload_grants']) is list,
                'Expected arrays')
        grants = []
        for grant in row['upload_grants']:
            exact_fields(grant, UploadGrant.__dataclass_fields__)
            grants.append(UploadGrant(**grant))
        devices.append(Device(**{**row, 'capabilities': tuple(row['capabilities']),
                                 'upload_grants': tuple(grants)}))
    frozen = tuple(devices)
    return Registry(1, _digest(frozen), frozen)


def select_devices(registry: Registry, selected: Sequence[str], *, purpose: str) -> tuple[Device, ...]:
    require(type(purpose) is str and purpose in ('monitor', 'receive', 'standardize'), 'Invalid purpose')
    require(isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)) and
            len(selected) > 0, 'Explicit nonempty selection required')
    for item in selected:
        _identity(item)
    require(len(set(selected)) == len(selected), 'Duplicate selection')
    indexed = {d.device_id: d for d in registry.devices}
    result = []
    for device_id in selected:
        require(device_id in indexed, 'Unknown device')
        device = indexed[device_id]
        require(device.enrolled and (device.monitor_enabled if purpose == 'monitor'
                                     else device.receive_member), 'Device purpose denied')
        result.append(device)
    return tuple(result)


def receive_ids(registry: Registry) -> frozenset[str]:
    return frozenset(d.device_id for d in registry.devices if d.enrolled and d.receive_member)


def upload_allowed(registry: Registry, device_id: str, *, destination: str, now_utc: datetime) -> bool:
    require(isinstance(now_utc, datetime) and now_utc.utcoffset() == timedelta(0),
            'Aware UTC now_utc required')
    if type(destination) is not str or destination not in UPLOAD_DESTINATIONS:
        return False
    for device in registry.devices:
        if device.device_id == device_id and device.enrolled:
            return any(g.operation == 'upload' and g.destination == destination and
                       (g.expiry is None or utc_timestamp(g.expiry) > now_utc)
                       for g in device.upload_grants)
    return False
