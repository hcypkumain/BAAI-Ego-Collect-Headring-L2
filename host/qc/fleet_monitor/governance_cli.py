"""Explicit-target, read-only local CLI. No device/config access on import or help."""
import argparse
import importlib
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

from host.common.device_registry import parse_registry, select_devices
from host.common.governance_contract import (COMPONENT_IDS, FIELD_IDS, Evidence, ProbeResult,
                                             validate_probe_policy, validate_probe_result)
from host.common.strict_json import canonical_json, require, strict_json_loads

_INPUT_LIMIT = 1048576
_TRUSTED_SOURCES = frozenset(('proc:boot_id', 'proc:meminfo', 'proc:memavailable',
                             'policy:disk_mount', 'ssh:identity', 'host:deadline'))
_UNCLOSED = []  # Strong references: a failed close must never become a fresh retry namespace.


class CliError(Exception):
    """Only a fixed public code, never adapter exception text."""


def _read_json(path):
    # Explicitly requested local registry/policy only. Trust files are NOT opened.
    target = Path(path).absolute()
    require('..' not in target.parts, 'Canonical input path required')
    fd = os.open(target.parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(target.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        try:
            require(stat.S_ISREG(os.fstat(source).st_mode), 'Regular input file required')
            with os.fdopen(source, 'rb', closefd=False) as stream:
                data = stream.read(_INPUT_LIMIT + 1)
        finally:
            os.close(source)
    finally:
        os.close(fd)
    require(len(data) <= _INPUT_LIMIT, 'Input exceeds bound')
    return strict_json_loads(data.decode('utf-8'))


def _targets(value):
    values = value.split(',')
    if (not all(re.fullmatch(r'cp_[0-9]{4}', item) for item in values) or
            len(set(values)) != len(values) or len(values) > 64):
        raise argparse.ArgumentTypeError('TARGETS_INVALID')
    return tuple(values)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--registry', required=True, help='Explicit local registry JSON')
    result.add_argument('--targets', required=True, type=_targets, help='Explicit comma-separated IDs')
    result.add_argument('--policy', required=True, help='Explicit local probe policy JSON')
    result.add_argument('--once', required=True, action='store_true', help='One bounded observation only')
    result.add_argument('--format', choices=('json',), default='json')
    return result


def _adapters():
    try:
        probe = importlib.import_module('host.qc.fleet_monitor.readonly_probe')
        transport = importlib.import_module('host.qc.fleet_monitor.isolated_transport')
        bom = importlib.import_module('tools.fleet_maintenance.component_bom')
    except ModuleNotFoundError as exc:
        raise CliError('ADAPTER_NOT_INTEGRATED') from exc
    # No compatibility shim: final import locations must match C/B delivery docs.
    scheduler = getattr(transport, 'ProbeScheduler', None)
    if (not callable(scheduler) or not callable(getattr(probe, 'probe_once', None)) or
            not callable(getattr(bom, 'compare_components', None))):
        raise CliError('ADAPTER_NOT_INTEGRATED')
    return scheduler, bom.compare_components


def _unknown(digest, device_id, generation, status='timeout'):
    return ProbeResult(digest, device_id, generation, False, status,
                       tuple((name, Evidence(None, 'host:deadline', None, 'unknown', None))
                             for name in FIELD_IDS))


def _source(label):
    # Preserve only reviewed logical labels. Never echo private paths/raw stderr.
    return label if label in _TRUSTED_SOURCES else 'adapter:label-withheld'


def _render_evidence(name, evidence):
    value = None if evidence.value_json is None else strict_json_loads(evidence.value_json)
    quality = evidence.quality
    if name.endswith('_components') and value is not None:
        value = {key: {**row, 'source': _source(row['source'])} for key, row in value.items()}
    if name == 'controller_state' and value not in (None, 'idle', 'busy', 'recording',
                                                   'stopped', 'starting', 'stopping', 'error'):
        value, quality = None, 'unknown'
    if name == 'boot_id' and value is not None and not re.fullmatch(
            r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value):
        value, quality = None, 'unknown'
    return {'value': value, 'source': _source(evidence.source),
            'observed_at': evidence.observed_at, 'quality': quality,
            'reason': None if quality == 'observed' else 'EVIDENCE_' + quality.upper(),
            # Remote wall time and local receipt time cannot prove sample age.
            'age_seconds': None, 'freshness': 'unknown'}


def _render_comparison(comparison):
    """B returns a complete BOM copy; display only reviewed, typed public columns."""
    require(type(comparison) is dict and type(comparison.get('components')) is list,
            'Invalid component comparison')
    indexed = {}
    for row in comparison['components']:
        require(type(row) is dict and type(row.get('component_id')) is str and
                row['component_id'] in COMPONENT_IDS and row['component_id'] not in indexed,
                'Invalid compared component')
        indexed[row['component_id']] = row
    require(set(indexed) == set(COMPONENT_IDS), 'Incomplete component comparison')
    result = {}
    for name in COMPONENT_IDS:
        row = indexed[name]
        output = {}
        for phase in ('source', 'installed', 'runtime'):
            value = row.get(phase, {})
            require(type(value) is dict, 'Invalid compared phase')
            sha = value.get('sha256')
            valid_hash = type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha) is not None
            status = value.get('status', 'unknown')
            known = valid_hash and (status == 'observed' or phase == 'source' and
                                    status in ('verified', 'known'))
            output[phase] = {'sha256': sha if known else None,
                             'status': 'observed' if known else 'unknown',
                             'scope': 'selected-file-set' if phase == 'source' else 'file-evidence-only'}
        result[name] = output
    return result


def _stats(scheduler):
    value = scheduler.stats()
    required = ('active', 'quarantined', 'workers', 'children', 'queued', 'bytes')
    require(type(value) is dict and all(type(value.get(key)) is int and value[key] >= 0
                                       for key in required), 'Invalid scheduler stats')
    require(value['active'] <= 2 and all(value[key] <= 64 for key in
            ('quarantined', 'workers', 'children', 'queued')) and value['bytes'] <= 256 * 1048576,
            'Scheduler budget exceeded')
    return {key: value[key] for key in required}


def observe(devices, *, registry_digest, policy, scheduler_factory, compare_components, bom):
    """One bounded generation using the fixed C scheduler API; injected only by main/tests."""
    require(bool(devices) and len(devices) <= 64 and
            len({d.device_id for d in devices}) == len(devices) and
            all(d.enrolled and d.monitor_enabled for d in devices), 'Invalid selected devices')
    if _UNCLOSED:
        raise CliError('PRIOR_CLOSE_UNREAPED')
    generation = 1  # One local request namespace; watch/repeated generations are not implemented.
    expected = {device.device_id for device in devices}
    received = {}
    rejected = 0
    started = time.monotonic()
    deadline = started + (math.ceil(len(devices) / 2) + 1) * (policy.timeout_ms / 1000 + 1) + 2
    scheduler = scheduler_factory(tuple(devices), registry_digest=registry_digest,
                                  policy=policy, concurrency=2)
    closed = False
    stats = None
    try:
        while time.monotonic() < deadline:
            scheduler.tick(time.monotonic())
            stats = _stats(scheduler)
            # Do not drain staged RAM early: C coalesces final stages and suppresses
            # another attempt until drain. This preserves exactly one attempt/device.
            if stats['queued'] >= len(expected) and stats['active'] == 0:
                break
            time.sleep(0.01)
        for candidate in scheduler.drain():
            try:
                result = validate_probe_result(candidate)
                require(result.registry_digest == registry_digest and
                        result.device_id in expected and result.generation == generation and
                        result.device_id not in received, 'Unexpected response identity')
            except (ValueError, TypeError):
                rejected += 1
                continue
            received[result.device_id] = result
    finally:
        try:
            closed = scheduler.close(timeout_s=2.0)
        finally:
            if not closed:
                _UNCLOSED.append(scheduler)
    if not closed:
        raise CliError('CLOSE_NOT_REAPED')
    stats = _stats(scheduler)
    output = []
    for device in devices:
        result = received.get(device.device_id, _unknown(registry_digest, device.device_id, generation))
        comparison = compare_components(bom, result)
        output.append({'device_id': device.device_id, 'generation': generation,
                       'identity_verified': result.identity_verified, 'status': result.status,
                       'fields': {name: _render_evidence(name, evidence) for name, evidence in result.fields},
                       'component_comparison': _render_comparison(comparison)})
    warnings = []
    if stats['quarantined']:
        warnings.append('DEVICE_QUARANTINED')
    if rejected:
        warnings.append('REJECTED_RESULTS')
    if expected - received.keys():
        warnings.append('REQUEST_DEADLINE')
    if any(row['status'] != 'ok' or any(field['quality'] != 'observed' or field['value'] is None
                                      for field in row['fields'].values()) for row in output):
        warnings.append('PARTIAL_EVIDENCE')
    return {'schema_version': 1, 'registry_digest': registry_digest, 'generation': generation,
            'observations': output, 'rejected_results': rejected, 'closed': closed,
            'stats': stats, 'warning_codes': warnings,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'production_pipeline_migrated': False}


def _packaged_bom():
    # Build installs BOM beside the explicit package payload; never reads a live default config.
    path = Path(__file__).resolve().parents[3] / 'BOM.json'
    if not path.is_file():
        raise CliError('RELEASE_NOT_BUILT')
    value = _read_json(path)
    require(type(value) is dict and type(value.get('component_BOM')) is dict, 'Invalid built BOM')
    return value['component_BOM']


def main(argv=None):
    args = parser().parse_args(argv)  # help/missing/duplicate targets: zero config reads.
    try:
        registry = parse_registry(_read_json(args.registry))
        devices = select_devices(registry, args.targets, purpose='monitor')
        policy = validate_probe_policy(_read_json(args.policy))
    except (ValueError, TypeError, OSError, UnicodeError):
        print(canonical_json({'error': 'INPUT_REJECTED'}), file=sys.stderr)
        return 2
    try:
        if sys.implementation.name != 'cpython' or sys.version_info[:3] != (3, 14, 4):
            raise CliError('RUNTIME_NOT_VALIDATED')
        scheduler, compare = _adapters()
        output = observe(devices, registry_digest=registry.digest, policy=policy,
                         scheduler_factory=scheduler, compare_components=compare, bom=_packaged_bom())
        print(canonical_json(output))
        return 5 if output['warning_codes'] else 0
    except KeyboardInterrupt:
        print(canonical_json({'error': 'INTERRUPTED'}), file=sys.stderr)
        return 130
    except CliError as exc:
        code = str(exc)
        if code not in ('ADAPTER_NOT_INTEGRATED', 'RELEASE_NOT_BUILT', 'CLOSE_NOT_REAPED',
                        'PRIOR_CLOSE_UNREAPED', 'RUNTIME_NOT_VALIDATED'):
            code = 'OBSERVATION_FAILED'
        print(canonical_json({'error': code}), file=sys.stderr)
        return 3
    except Exception:
        # Do not expose registry paths, SSH stderr, callback or credential contents.
        print(canonical_json({'error': 'OBSERVATION_FAILED'}), file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
