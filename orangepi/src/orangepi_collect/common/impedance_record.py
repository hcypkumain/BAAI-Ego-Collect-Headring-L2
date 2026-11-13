"""Bounded, durable per-episode impedance evidence. Python 3.8, stdlib only."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time
from impedance_estimator import ALGORITHM, SDK_REFERENCE, NORMAL_RECOVERY_MAX_SAMPLES
from impedance_quality import (POLICY, THRESHOLD_KOHM, assess_impedance, quality_matches,
    acceptance_policy, quality_allows_recording, ACCEPTANCE_KEY, ADVISORY)

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 65536
RELATIVE_PATH = 'eeg/impedance_preflight.json'
USED_PATH = 'eeg/impedance_used.json'
METHOD = 'estimated_from_confirmed_excitation'
LEGACY_ALGORITHM = 'gui062-sin-cos-dc-31.25hz-v1'


class ImpedanceRecordError(ValueError):
    """Evidence is missing, incomplete, belongs elsewhere, or cannot be saved."""


def _encode(value):
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))+'\n').encode('utf-8')
    if len(payload) > MAX_RECORD_BYTES:
        raise ImpedanceRecordError('impedance record exceeds bounded size')
    return payload


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _sync_directory(path):
    if os.name == 'nt':
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_record(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _encode(value)
    temporary = path.with_suffix(path.suffix+'.tmp')
    try:
        with temporary.open('wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    except OSError as exc:
        raise ImpedanceRecordError('cannot persist impedance evidence: '+str(exc)) from exc
    return hashlib.sha256(payload).hexdigest()


def append_event(output, state, event, durable=False, stream='attempts'):
    if not output:
        return
    if stream not in ('attempts', 'samples'):
        raise ImpedanceRecordError('unknown impedance log')
    root = Path(output)
    value = copy.deepcopy(state)
    value.update(schema_version=SCHEMA_VERSION, session_id=root.name, event=event,
                 time_unix_ns=time.time_ns(), time_monotonic_ns=time.monotonic_ns(),
                 units='kOhm', method=METHOD)
    payload = _encode(value)
    path = root/'eeg'/('impedance_'+stream+'.jsonl')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('ab') as handle:
            handle.write(payload)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        if durable:
            _sync_directory(path.parent)
    except OSError as exc:
        raise ImpedanceRecordError('cannot persist impedance '+stream+': '+str(exc)) from exc


def save_preflight(output, state, frontend, device_binding):
    if not output:
        return copy.deepcopy(state)
    root = Path(output)
    value = copy.deepcopy(state)
    value.update(schema_version=SCHEMA_VERSION, session_id=root.name, units='kOhm',
                 method=METHOD, restored_frontend=frontend,
                 config_fingerprint=fingerprint(frontend), device_binding=device_binding,
                 algorithm=state.get('algorithm', LEGACY_ALGORITHM),
                 estimator_frequency_hz=31.25, window_seconds_max=4)
    enabled = [c for c in value.get('channels', []) if c.get('enabled')]
    value['expected_channel_mask'] = frontend.get('enabled_mask')
    value['quality'] = assess_impedance(value, expected_mask=frontend.get('enabled_mask'))
    valid_count = value['quality']['valid_count']
    value.update(enabled_channel_count=len(enabled), valid_channel_count=valid_count,
                 measurement_quality='complete' if enabled and valid_count == len(enabled) else 'partial')
    # Commit all preceding summaries before publishing a durable reviewed snapshot.
    try:
        for name in ('impedance_samples.jsonl', 'impedance_attempts.jsonl'):
            path = root/'eeg'/name
            if path.exists():
                with path.open('ab') as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
    except OSError as exc:
        raise ImpedanceRecordError('cannot flush impedance log: '+str(exc)) from exc
    atomic_record(root/RELATIVE_PATH, value)
    return value


def _read(root, relative):
    path = Path(root)/relative
    try:
        with path.open('rb') as handle:
            payload = handle.read(MAX_RECORD_BYTES+1)
        if len(payload) > MAX_RECORD_BYTES:
            raise ImpedanceRecordError('impedance evidence exceeds bounded size')
        value = json.loads(payload.decode('utf-8'),
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ImpedanceRecordError('cannot read impedance evidence '+relative+': '+str(exc)) from exc
    if not isinstance(value, dict):
        raise ImpedanceRecordError('impedance evidence must be an object')
    return value, hashlib.sha256(payload).hexdigest()


def _positive_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _validate_sdk_details(value, frontend, channels):
    """New SDK records have stronger evidence; old records keep their rules."""
    if value.get('sdk_reference') != SDK_REFERENCE:
        raise ImpedanceRecordError('impedance SDK source identity mismatch')
    if (value.get('sample_rate_hz') != 250 or value.get('window_seconds') != 4
            or value.get('window_required_positions') != 1000
            or value.get('window_ready') is not True
            or value.get('window_status') != 'ready'
            or value.get('window_positions') != 1000):
        raise ImpedanceRecordError('SDK impedance requires a complete four-second window')
    start = value.get('window_start_position')
    end = value.get('window_end_position')
    span = value.get('window_observed_span')
    received = value.get('window_received_samples')
    if (any(type(number) is not int for number in (start, end, span, received))
            or start < 0 or end-start+1 != 1000 or span < 1000
            or not 0 <= received <= 1000):
        raise ImpedanceRecordError('SDK impedance sample-position evidence mismatch')
    reference = frontend.get('reference')
    series = value.get('series_kohm')
    expected_series = 4.40 if reference == 'srb2' else 9.98
    if (reference not in ('srb1', 'srb2') or value.get('input_side') != reference
            or isinstance(series, bool) or not isinstance(series, (int, float))
            or not math.isfinite(series) or abs(series-expected_series) > 1e-12):
        raise ImpedanceRecordError('SDK impedance series/reference mismatch')
    for channel in channels:
        accepted = channel['accepted_samples']
        if accepted > received or (channel.get('value_kohm') is not None and accepted < 250):
            raise ImpedanceRecordError('SDK impedance has insufficient valid samples')
    measurement_generation = value.get('measurement_generation')
    normal_generation = value.get('normal_frame_generation')
    normal_time = value.get('normal_frame_monotonic_ns')
    received_at = value.get('normal_frame_received_at')
    consumed = value.get('normal_recovery_samples_consumed')
    if (value.get('normal_frame_confirmed') is not True
            or type(measurement_generation) is not int or measurement_generation < 0
            or type(normal_generation) is not int or normal_generation < 0
            or normal_generation == measurement_generation
            or not _positive_number(normal_time) or not _positive_number(received_at)
            or not value['measurement_started_monotonic_ns'] <= normal_time <= value['completed_monotonic_ns']
            or received_at*1e9 > normal_time+10000
            or received_at*1e9 < value['measurement_started_monotonic_ns']
            or type(consumed) is not int or not 1 <= consumed <= NORMAL_RECOVERY_MAX_SAMPLES):
        raise ImpedanceRecordError('SDK impedance normal-frame recovery is unconfirmed')


def validate_preflight(root, expected_state=None, expected_eeg_config=None):
    """Validate a NEW START under the current project policy. No I/O mutation."""
    return _validate_preflight(root, expected_state, expected_eeg_config,
                               require_project_quality=True)


def _validate_preflight(root, expected_state=None, expected_eeg_config=None,
                        require_project_quality=True):
    """Historical binding checks preserve the policy actually recorded then."""
    root = Path(root)
    value, digest = _read(root, RELATIVE_PATH)
    policy = acceptance_policy(value)
    advisory = policy == ADVISORY
    for expected in (expected_state, expected_eeg_config):
        if expected is not None and acceptance_policy(expected) != policy:
            raise ImpedanceRecordError('impedance acceptance policy differs from current owner/configuration')
    if (require_project_quality and advisory and expected_state is None
            and expected_eeg_config is None):
        raise ImpedanceRecordError('advisory START requires its configured owner or recording configuration')
    if (value.get('schema_version') != SCHEMA_VERSION or value.get('session_id') != root.name
            or value.get('units') != 'kOhm' or value.get('method') != METHOD):
        raise ImpedanceRecordError('impedance evidence schema/session/units mismatch')
    if value.get('algorithm') not in (ALGORITHM, LEGACY_ALGORITHM):
        raise ImpedanceRecordError('unsupported impedance algorithm identity')
    if (value.get('phase') != 'completed' or any(value.get(k) is not True for k in
            ('required', 'supported', 'reviewed', 'can_record', 'loff_off_confirmed', 'frontend_restored'))
            or value.get('active') or value.get('pending') or value.get('error')):
        raise ImpedanceRecordError('impedance check/review/recovery is incomplete')
    if (isinstance(expected_state, dict) and expected_state.get('algorithm') == ALGORITHM
            and value.get('algorithm') != ALGORITHM):
        raise ImpedanceRecordError('new SDK impedance evidence cannot downgrade to a legacy record')
    epoch = value.get('measurement_epoch')
    if not isinstance(epoch, str) or not 0 < len(epoch) <= 64:
        raise ImpedanceRecordError('missing measurement epoch')
    for key in ('measurement_started_at', 'completed_at', 'reviewed_at',
                'measurement_started_monotonic_ns', 'completed_monotonic_ns', 'reviewed_monotonic_ns'):
        if not _positive_number(value.get(key)):
            raise ImpedanceRecordError('missing impedance time: '+key)
    if not (value['measurement_started_monotonic_ns'] <= value['completed_monotonic_ns']
            <= value['reviewed_monotonic_ns']):
        raise ImpedanceRecordError('impedance phase times are out of order')
    if not value['measurement_started_at'] <= value['completed_at'] <= value['reviewed_at']:
        raise ImpedanceRecordError('impedance wall-clock phase times are out of order')
    frontend = value.get('restored_frontend')
    if (not isinstance(frontend, dict) or frontend.get('verified') is not True
            or frontend.get('enabled_mask') not in (254, 255) or frontend.get('mode') not in ('eeg', 'both_bias')
            or fingerprint(frontend) != value.get('config_fingerprint')):
        raise ImpedanceRecordError('impedance frontend fingerprint mismatch')
    channels = value.get('channels')
    if not isinstance(channels, list) or len(channels) != 8:
        raise ImpedanceRecordError('impedance requires explicit eight-channel mapping')
    valid = 0
    for index, channel in enumerate(channels, 1):
        if (not isinstance(channel, dict) or channel.get('channel') != index
                or not isinstance(channel.get('label'), str)
                or type(channel.get('enabled')) is not bool
                or channel['enabled'] != bool(frontend.get('enabled_mask', 0) & (1 << (index-1)))):
            raise ImpedanceRecordError('impedance channel/mask mismatch')
        number = channel.get('value_kohm')
        accepted = channel.get('accepted_samples')
        if type(accepted) is not int or accepted < 0:
            raise ImpedanceRecordError('invalid accepted impedance sample count')
        if number is not None:
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or not math.isfinite(number) or number < 0 or not channel['enabled']
                    or channel.get('reason') != 'ok' or accepted == 0):
                raise ImpedanceRecordError('invalid impedance channel value')
            valid += 1
        elif not isinstance(channel.get('reason'), str) or not channel['reason']:
            raise ImpedanceRecordError('missing invalid-channel reason')
    if not valid and not advisory:
        raise ImpedanceRecordError('no valid impedance channel; repeat measurement')
    if value.get('algorithm') == ALGORITHM:
        _validate_sdk_details(value, frontend, channels)
    enabled_count = sum(c['enabled'] for c in channels)
    if (value.get('enabled_channel_count') != enabled_count
            or value.get('valid_channel_count') != valid
            or value.get('measurement_quality') != ('complete' if valid == enabled_count else 'partial')):
        raise ImpedanceRecordError('impedance summary disagrees with channel values')
    declared_quality = value.get('quality')
    # Old evidence is still checkable as historical evidence, but it never
    # silently becomes a newly accepted 60 kOhm result or authorizes a START.
    if require_project_quality or declared_quality is not None:
        quality = assess_impedance(value, expected_mask=frontend['enabled_mask'])
        if not quality_matches(declared_quality, quality):
            raise ImpedanceRecordError('impedance project policy evidence is missing or inconsistent; repeat measurement')
        if require_project_quality and not quality_allows_recording(value):
            raise ImpedanceRecordError('impedance measurement incomplete or invalid; repeat measurement')
        if not advisory and quality['passed'] is not True:
            raise ImpedanceRecordError('impedance not passed: every enabled channel must be valid and below 60 kOhm')
    if expected_state is not None:
        if (expected_state.get('measurement_epoch') != epoch
                or expected_state.get('config_fingerprint') != value['config_fingerprint']
                or expected_state.get('can_record') is not True
                or expected_state.get('reviewed') is not True):
            raise ImpedanceRecordError('persisted impedance does not match current owner')
        if require_project_quality:
            owner_quality = assess_impedance(expected_state, expected_mask=frontend['enabled_mask'])
            if ((not advisory and owner_quality['passed'] is not True)
                    or not quality_matches(expected_state.get('quality'), owner_quality)):
                raise ImpedanceRecordError('current owner impedance quality is not passed')
    if expected_eeg_config is not None:
        config = expected_eeg_config
        if not isinstance(config, dict):
            raise ImpedanceRecordError('recording EEG configuration must be an object')
        disabled = config.get('disabled_channels', [1])
        if (config.get('sample_rate_hz', 250) != 250
                or disabled not in ([], [1])
                or frontend['enabled_mask'] != (255 if disabled == [] else 254)):
            raise ImpedanceRecordError('recording EEG profile differs from impedance profile')
        if [c['label'] for c in channels] != config.get('labels'):
            raise ImpedanceRecordError('impedance channel labels differ from recording')
        binding = value.get('device_binding', {})
        if not isinstance(binding, dict):
            raise ImpedanceRecordError('impedance device binding must be an object')
        if (binding.get('port') != config.get('port')
                or binding.get('configured_usb_serial') != config.get('serial')):
            raise ImpedanceRecordError('impedance device binding differs from recording')
    proof = dict(relative_path=RELATIVE_PATH, preflight_sha256=digest,
                measurement_epoch=epoch, reviewed_at=value['reviewed_at'],
                reviewed_monotonic_ns=value['reviewed_monotonic_ns'],
                config_fingerprint=value['config_fingerprint'], used_relative_path=USED_PATH)
    if declared_quality is not None:
        proof.update(quality_policy=POLICY, quality_threshold_kohm=THRESHOLD_KOHM,
                     quality_comparison='lt')
    if ACCEPTANCE_KEY in value:
        proof[ACCEPTANCE_KEY] = policy
    return proof


def bind_start(root, start_gate, expected_state=None, expected_eeg_config=None):
    """Durably freeze which preflight the forthcoming START uses; return its proof."""
    proof = validate_preflight(root, expected_state, expected_eeg_config)
    start = start_gate.get('start_monotonic_ns')
    if (not _positive_number(start) or not _positive_number(start_gate.get('start_unix_ns'))
            or proof['reviewed_monotonic_ns'] > start
            or proof['reviewed_at']*1e9 > start_gate['start_unix_ns']):
        raise ImpedanceRecordError('START precedes the reviewed impedance check')
    used = dict(schema_version=SCHEMA_VERSION, session_id=Path(root).name,
                start_monotonic_ns=start, start_unix_ns=start_gate['start_unix_ns'],
                eeg_impedance=proof)
    atomic_record(Path(root)/USED_PATH, used)
    return proof


def validate_start_binding(root, start_gate=None, expected_eeg_config=None):
    """QC/export validator; return proof or raise ImpedanceRecordError."""
    if start_gate is None:
        start_gate, _ = _read(root, 'start_gate.json')
    proof = _validate_preflight(root, expected_eeg_config=expected_eeg_config,
                                require_project_quality=False)
    if not isinstance(start_gate, dict) or start_gate.get('eeg_impedance') != proof:
        raise ImpedanceRecordError('START has no matching persisted impedance proof')
    used, _ = _read(root, USED_PATH)
    if (used.get('schema_version') != SCHEMA_VERSION or used.get('session_id') != Path(root).name
            or used.get('eeg_impedance') != proof
            or used.get('start_monotonic_ns') != start_gate.get('start_monotonic_ns')
            or used.get('start_unix_ns') != start_gate.get('start_unix_ns')
            or not _positive_number(start_gate.get('start_monotonic_ns'))
            or proof['reviewed_monotonic_ns'] > start_gate['start_monotonic_ns']
            or not _positive_number(start_gate.get('start_unix_ns'))
            or proof['reviewed_at']*1e9 > start_gate['start_unix_ns']):
        raise ImpedanceRecordError('adopted impedance record differs from START')
    return proof
