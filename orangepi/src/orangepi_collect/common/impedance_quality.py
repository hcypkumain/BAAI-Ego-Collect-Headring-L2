"""Project impedance acceptance policy; no device I/O or numerical estimation.

The 60 kOhm limit is a project rule, not a universal EEG quality standard.
Only configured channel-to-reference estimates are evaluated. The reference
and BIAS electrodes are not independently measured by this SDK path.
"""
import math

POLICY = 'all_enabled_impedance_lt_60kohm_v1'
THRESHOLD_KOHM = 60.0
EXPECTED_MASK = 0xff
ACCEPTANCE_KEY = 'impedance_acceptance_policy'
STRICT = 'strict'
ADVISORY = 'advisory'
PENDING_PHASES = ('not_checked', 'starting', 'measuring', 'restoring', 'resuming',
                  'initializing', 'awaiting_prepare')


def acceptance_policy(value):
    """Keep the numeric result separate from the configured recording gate."""
    policy = (value or {}).get(ACCEPTANCE_KEY, STRICT)
    if policy not in (STRICT, ADVISORY):
        raise ValueError('unknown impedance acceptance policy')
    return policy


def quality_allows_recording(state):
    return acceptance_policy(state) == ADVISORY or assess_impedance(state).get('passed') is True


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def _complete_window(state):
    start, end = state.get('window_start_position'), state.get('window_end_position')
    span, received = state.get('window_observed_span'), state.get('window_received_samples')
    return (state.get('window_ready') is True and state.get('window_status') == 'ready'
            and state.get('sample_rate_hz') == 250 and state.get('window_seconds') == 4
            and state.get('window_required_positions') == 1000
            and state.get('window_positions') == 1000
            and all(type(value) is int for value in (start, end, span, received))
            and start >= 0 and end-start+1 == 1000 and span >= 1000
            and 250 <= received <= 1000)


def assess_impedance(state, expected_mask=None):
    """Return JSON-safe fail-closed quality, without trusting cached quality.

    The owner/record validator supplies its confirmed frontend mask. Control
    clients can use the owner's expected_channel_mask, with the current
    eight-channel configuration as fallback before the owner reports a mask.
    Historical records supply their saved mask explicitly.
    Numeric per-channel results can pass during measurement; total passed
    remains false until the full window and normal-mode recovery complete.
    Manual review and the durable START proof remain separate requirements.
    """
    state = state if isinstance(state, dict) else {}
    mask = state.get('expected_channel_mask', EXPECTED_MASK) if expected_mask is None else expected_mask
    mask_ok = type(mask) is int and 0 < mask <= 0xff
    expected = [channel for channel in range(1, 9) if mask_ok and mask & (1 << (channel-1))]
    phase = state.get('phase')
    window_ready = _complete_window(state)
    stale = state.get('stale') is True or state.get('window_status') == 'stale'
    rows = state.get('channels')
    mapping, mapping_ok = {}, isinstance(rows, list)
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            mapping_ok = False
            continue
        channel = row.get('channel')
        if type(channel) is not int or not 1 <= channel <= 8 or channel in mapping:
            mapping_ok = False
            continue
        mapping[channel] = row
        enabled = bool(mask_ok and mask & (1 << (channel-1)))
        if type(row.get('enabled')) is not bool or row['enabled'] != enabled:
            mapping_ok = False
        if enabled and row.get('stale') is True:
            stale = True

    results = []
    received = state.get('window_received_samples')
    for channel in range(1, 9):
        row = mapping.get(channel, {})
        enabled = channel in expected
        value = row.get('value_kohm')
        value = float(value) if _number(value) else None
        reason = row.get('reason')
        accepted = row.get('accepted_samples')
        sample_valid = (type(accepted) is int and accepted >= 250
                        and type(received) is int and accepted <= received)
        numeric_valid = (row.get('enabled') is True and reason == 'ok'
                         and value is not None and sample_valid)
        if not enabled:
            result, detail = 'disabled', 'not_in_enabled_mask'
        elif channel not in mapping:
            result = 'pending' if phase in PENDING_PHASES and not window_ready else 'fail'
            detail = 'missing_channel'
        elif stale or row.get('stale') is True or reason == 'stale':
            result, detail = 'fail', 'stale'
        elif row.get('enabled') is not True:
            result, detail = 'fail', 'enabled_channel_not_enabled'
        elif not numeric_valid:
            waiting = reason in ('waiting', 'pending_window', 'generation_changed', 'delivery_outage')
            result = 'pending' if waiting and phase in PENDING_PHASES else 'fail'
            detail = str(reason or 'invalid_result') if value is None or reason != 'ok' else 'insufficient_valid_samples'
        elif value >= THRESHOLD_KOHM:
            result, detail = 'fail', 'at_or_above_threshold'
        elif not window_ready:
            result, detail = 'pending', 'incomplete_window'
        else:
            result, detail = 'pass', 'below_threshold'
        results.append(dict(channel=channel, label=str(row.get('label', 'CH'+str(channel))),
                            enabled=enabled, state=result, passed=result == 'pass',
                            value_kohm=value if enabled else None, reason=detail))

    enabled_results = [row for row in results if row['enabled']]
    failed = [row['channel'] for row in enabled_results if row['state'] == 'fail']
    pending = [row['channel'] for row in enabled_results if row['state'] == 'pending']
    valid_count = sum(1 for channel in expected if channel in mapping
        and mapping[channel].get('enabled') is True
        and mapping[channel].get('reason') == 'ok'
        and _number(mapping[channel].get('value_kohm'))
        and type(mapping[channel].get('accepted_samples')) is int
        and mapping[channel]['accepted_samples'] >= 250
        and type(received) is int and mapping[channel]['accepted_samples'] <= received
        and not stale and mapping[channel].get('stale') is not True)
    passed_count = sum(row['passed'] for row in enabled_results)
    if state.get('required') is not True or state.get('supported') is not True:
        overall, detail = 'unavailable', 'measurement_unavailable'
    elif not mask_ok or not expected:
        overall, detail = 'fail', 'no_expected_channels'
    elif not mapping_ok:
        overall, detail = 'fail', 'channel_mapping_mismatch'
    elif stale:
        overall, detail = 'fail', 'stale'
    elif state.get('error') or phase in ('fault', 'cancelled', 'interrupted'):
        overall, detail = 'fail', 'measurement_error'
    elif not window_ready:
        overall = 'pending' if phase in PENDING_PHASES else 'fail'
        detail = 'incomplete_window'
    elif phase != 'completed' or state.get('active') is not False or state.get('pending') is not False:
        overall, detail = 'pending', 'measurement_not_completed'
    elif any(state.get(key) is not True for key in
             ('loff_off_confirmed', 'frontend_restored', 'normal_frame_confirmed')):
        overall, detail = 'pending', 'normal_acquisition_recovery_unconfirmed'
    elif failed or pending or passed_count != len(expected):
        overall, detail = 'fail', 'channels_not_passed'
    else:
        overall, detail = 'pass', 'all_enabled_channels_below_threshold'
    return dict(schema_version=1, policy=POLICY, threshold_kohm=THRESHOLD_KOHM,
                comparison='lt', passed=overall == 'pass', state=overall,
                expected_mask=mask if mask_ok else 0, expected_count=len(expected),
                valid_count=valid_count, passed_count=passed_count,
                failed_channels=failed, pending_channels=pending, reason=detail,
                channels=results, window_ready=window_ready,
                measurement_scope='enabled_channel_to_reference_estimate',
                reference_electrode_independently_measured=False,
                bias_electrode_independently_measured=False)


def quality_matches(value, expected):
    """Validate complete persisted policy evidence, not just a cached passed bit."""
    if (not isinstance(value, dict) or type(value.get('passed')) is not bool
            or any(type(value.get(key)) is not int for key in
                   ('schema_version', 'expected_mask', 'expected_count', 'valid_count', 'passed_count'))
            or not isinstance(value.get('channels'), list)
            or any(not isinstance(row, dict) or type(row.get('passed')) is not bool
                   or type(row.get('enabled')) is not bool for row in value['channels'])):
        return False
    return value == expected
