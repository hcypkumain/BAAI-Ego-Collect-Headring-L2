"""Read-only modality-specific integrity checks, independent of practical-v1.

The checks never resample, repair timestamps, delete files or operate hardware.
Counter-derived EMG intervals describe an inferred transmitter timeline, not a
hardware-synchronized ADC clock. Receipt clocks are never called sample gaps.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat

import numpy as np
from embodied_brain_collect.checkers.base import BaseCheck, CheckOutput, Span

try:
    from .gap_thresholds import (WRIST_WARN_NS, WRIST_ERROR_NS, EMG_WARN_NS,
                                 EMG_ERROR_NS, EVENT_LIMIT, emg_loss_level)
except ImportError:
    from gap_thresholds import (WRIST_WARN_NS, WRIST_ERROR_NS, EMG_WARN_NS,
                                EMG_ERROR_NS, EVENT_LIMIT, emg_loss_level)

WRISTS = frozenset(('cam_left_wrist', 'cam_right_wrist'))
EMGS = frozenset(('emg_left', 'emg_right'))


def _finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _integer_array(value, label, size=None):
    if value is None:
        raise ValueError(label + '_missing')
    a = np.asarray(value)
    if a.ndim != 1 or a.dtype.kind not in 'iu':
        raise ValueError(label + '_not_an_integer_vector')
    if size is not None and a.size != size:
        raise ValueError(label + '_length_mismatch')
    if a.size and (np.any(a < 0) or np.any(a > np.iinfo(np.int64).max)):
        raise ValueError(label + '_outside_nonnegative_int64')
    return a.astype(np.int64, copy=False)


def _seconds_array(value, label, size=None):
    if value is None:
        raise ValueError(label + '_missing')
    a = np.asarray(value)
    if a.ndim != 1 or a.dtype.kind not in 'ifu' or not np.all(np.isfinite(a)):
        raise ValueError(label + '_not_a_finite_numeric_vector')
    if size is not None and a.size != size:
        raise ValueError(label + '_length_mismatch')
    return a.astype(np.float64, copy=False)


def _window_mask(ctx, epoch):
    if ctx.window is None:
        return np.ones(len(epoch), dtype=bool)
    w = ctx.window
    if (not isinstance(w, dict) or not _finite_number(w.get('t0'))
            or not _finite_number(w.get('t1')) or w['t1'] < w['t0']):
        raise ValueError('recording_window_invalid')
    return (epoch >= w['t0']) & (epoch <= w['t1'])


def _unknown(check, out, reason, **detail):
    out.stats.update(status='error' if out.level == 'ERROR' else 'unknown',
                     passed=False if out.level == 'ERROR' else None,
                     timing_status='unknown', reason=reason)
    out.findings.append(check.finding('WARN', '时间完整性无法确认：' + reason,
        detail=dict(status='unknown', reason=reason, **detail)))
    return out


def _invalid(check, out, reason, **detail):
    out.stats.update(status='error', passed=False, reason=reason)
    out.findings.append(check.finding('ERROR', '采集完整性异常：' + reason,
        detail=dict(status='invalid', reason=reason, **detail)))
    return out


def _gaps(clock, indices, epoch, warn_ns, error_ns):
    delta = np.diff(clock)
    warning = (delta > warn_ns) & (delta <= error_ns)
    error = delta > error_ns
    found = np.flatnonzero(warning | error)
    maximum = int(delta.max()) if delta.size else 0

    def event(i):
        return dict(before_index=int(indices[i]), after_index=int(indices[i + 1]),
                    before_unix_s=float(epoch[i]), after_unix_s=float(epoch[i + 1]),
                    offset_s=int(clock[i] - clock[0]) / 1e9,
                    interval_ns=int(delta[i]), interval_s=int(delta[i]) / 1e9,
                    level='ERROR' if delta[i] > error_ns else 'WARN')

    return dict(n_intervals=int(delta.size), warn_threshold_ns=warn_ns,
                error_threshold_ns=error_ns, comparison='strictly_greater_than',
                warn_count=int(warning.sum()), error_count=int(error.sum()),
                exceeds_warn_count=int(found.size), max_interval_ns=maximum,
                max_interval_s=maximum / 1e9,
                events=[event(int(i)) for i in found[:EVENT_LIMIT]],
                events_truncated=bool(found.size > EVENT_LIMIT), event_limit=EVENT_LIMIT,
                worst_event=event(int(np.argmax(delta))) if maximum > warn_ns else None)


def _add_gap_finding(check, out, facts, field, subject):
    if not facts['exceeds_warn_count']:
        return
    level = 'ERROR' if facts['error_count'] else 'WARN'
    threshold = facts['error_threshold_ns'] if level == 'ERROR' else facts['warn_threshold_ns']
    out.findings.append(check.finding(level,
        '%s 相邻帧间隔超限：WARN %d 次，ERROR %d 次，最大 %.3f ms' % (
            subject, facts['warn_count'], facts['error_count'], facts['max_interval_ns'] / 1e6),
        field=field, subject=subject, observed=facts['max_interval_s'], threshold=threshold / 1e9,
        detail=dict(facts), spans=[Span(t=e['before_unix_s'], dur=e['interval_s'],
             msg=e['level'] + ' gap') for e in facts['events'][:50]]))


@dataclass(frozen=True)
class PiWristFrameGap(BaseCheck):
    """Exact adjacent saved-frame checks; no preview or network FPS is used."""

    def run(self, ctx):
        out = CheckOutput(stats=dict(schema_version=1, status='unknown', passed=None,
                                    scope='recorded_wrist_frames', hardware_synchronized=False))
        try:
            epoch = _seconds_array(ctx.arr('frames_timestamps'), 'frames_timestamps')
            if epoch.size < 2:
                return _unknown(self, out, 'fewer_than_two_recorded_frames')
            source = ctx.arr('source_pts_ns')
            fallback = source is None
            field = 'arrival_monotonic_ns' if fallback else 'source_pts_ns'
            clock = _integer_array(ctx.arr(field), field, epoch.size)
            mask = _window_mask(ctx, epoch)
            indices = np.flatnonzero(mask)
            clock, epoch = clock[mask], epoch[mask]
            if clock.size < 2:
                return _unknown(self, out, 'fewer_than_two_recorded_frames_in_window')
            if np.any(np.diff(clock) <= 0):
                return _invalid(self, out, field + '_not_strictly_increasing')
        except (ValueError, TypeError, OverflowError) as exc:
            return _unknown(self, out, str(exc))
        out.stats.update(timestamp_field=field,
            clock_basis=('host_frame_arrival_monotonic_not_camera_exposure' if fallback
                         else 'saved_V4L2_GStreamer_buffer_PTS_not_hardware_sync'),
            n_frames=int(clock.size), window=ctx.window)
        facts = _gaps(clock, indices, epoch, WRIST_WARN_NS, WRIST_ERROR_NS)
        out.stats.update(facts)
        _add_gap_finding(self, out, facts, field, 'frames')
        if fallback:
            _unknown(self, out, 'source_PTS_missing_host_arrival_intervals_only', timestamp_field=field)
        else:
            out.stats.update(status='error' if facts['error_count'] else 'warn' if facts['warn_count'] else 'pass',
                             passed=not bool(facts['exceeds_warn_count']))
        return out


def _rebuild_record(directory):
    path = Path(directory) / 'timestamp_rebuild.json'
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 65536:
        raise ValueError('reconstruction_record_not_bounded_regular_file')
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or signature(opened) != signature(before):
            raise ValueError('reconstruction_record_changed')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(65537)
        if (len(raw) != before.st_size or signature(os.fstat(descriptor)) != signature(before)
                or signature(path.lstat()) != signature(before)):
            raise ValueError('reconstruction_record_changed')
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode('utf-8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('nonfinite_reconstruction_record')))
    if (not isinstance(value, dict) or value.get('ok') is not True
            or type(value.get('n_anchors')) is not int or value['n_anchors'] < 5
            or not _finite_number(value.get('emg_rate_hz')) or value['emg_rate_hz'] <= 0
            or not _finite_number(value.get('residual_ms')) or value['residual_ms'] < 0):
        raise ValueError('successful_sequence_reconstruction_not_evidenced')
    return value


@dataclass(frozen=True)
class PiEmgContinuity(BaseCheck):
    """Verify the shared wire counter before using a reconstructed timeline.

    Lost frame counts are lower bounds because the counter wraps after 256
    transmitted EMG+IMU frames. Arrival stalls only produce uncertainty notices.
    """

    def run(self, ctx):
        out = CheckOutput(stats=dict(schema_version=1, status='unknown', passed=None,
            clock_basis='shared_EMG_IMU_SN_fitted_to_host_arrival_not_hardware_sync',
            sequence_modulo=256, loss_scope='combined_EMG_IMU_wire_frames', loss_is_lower_bound=True))
        try:
            meta = _rebuild_record(ctx.dir)
            basis = ctx.arr('timestamp_basis')
            if basis is None or np.asarray(basis).shape != () or not str(np.asarray(basis).item()).startswith('sequence-fit to host arrival'):
                raise ValueError('sequence_fit_timestamp_basis_not_evidenced')
            raw = ctx.arr('raw_frames')
            if raw is None:
                raise ValueError('raw_shared_wire_frames_missing')
            raw = np.asarray(raw)
            if raw.ndim != 2 or raw.shape[1] != 29 or raw.shape[0] < 2 or raw.dtype.kind not in 'iu':
                raise ValueError('raw_shared_wire_frame_shape_invalid')
            if np.any(raw < 0) or np.any(raw > 255) or not np.all(raw[:, :3] == 0xD2):
                return _invalid(self, out, 'recorded_wire_frame_header_or_bytes_invalid')
            em = raw[:, 3] == 0xAA
            im = raw[:, 3] == 0xBB
            if not np.all(em | im):
                return _invalid(self, out, 'recorded_wire_frame_type_invalid')
            esn = _integer_array(ctx.arr('emg_sn'), 'emg_sn', int(em.sum()))
            isn = _integer_array(ctx.arr('imu_sn'), 'imu_sn', int(im.sum()))
            if not np.array_equal(esn, raw[em, 4]) or not np.array_equal(isn, raw[im, 4]):
                return _invalid(self, out, 'raw_shared_SN_does_not_match_EMG_IMU_arrays')
            et = _seconds_array(ctx.arr('emg_timestamps'), 'emg_timestamps', esn.size)
            it = _seconds_array(ctx.arr('imu_timestamps'), 'imu_timestamps', isn.size)
            epoch = np.empty(raw.shape[0], dtype=np.float64)
            epoch[em], epoch[im] = et, it
            if np.any(np.diff(epoch) <= 0):
                return _invalid(self, out, 'reconstructed_shared_timeline_not_strictly_increasing')
            sn = raw[:, 4].astype(np.int64)
            steps = np.diff(sn) % 256
            if np.any(steps == 0):
                raise ValueError('repeated_shared_SN_or_full_counter_wrap_ambiguous')
            index = np.r_[0, np.cumsum(steps)]
            period_ns = round((epoch[-1] - epoch[0]) * 1e9 / int(index[-1]))
            if not 10_000 <= period_ns <= 20_000_000:
                raise ValueError('reconstructed_wire_period_outside_supported_range')
            expected_rate = (1e9 / period_ns) * (esn.size / raw.shape[0])
            if abs(meta['emg_rate_hz'] / expected_rate - 1) > .02:
                raise ValueError('reconstruction_rate_and_saved_timeline_disagree')
            residual_ns = np.abs((epoch - epoch[0]) * 1e9 - index * period_ns)
            if residual_ns.max() > 5_000:
                raise ValueError('saved_timestamps_do_not_match_shared_sequence_linear_fit')
            mask = _window_mask(ctx, epoch)
            selected = np.flatnonzero(mask)
            if selected.size < 2:
                raise ValueError('fewer_than_two_wire_frames_in_recording_window')
        except (ValueError, TypeError, OverflowError, OSError, UnicodeError) as exc:
            return _unknown(self, out, str(exc))

        chosen = index[mask]
        missing = np.diff(chosen) - 1
        dropped = int(missing.sum())
        received = int(chosen.size)
        sent = received + dropped
        out.stats.update(window=ctx.window, inferred_wire_period_ns=int(period_ns),
            reconstruction_record='timestamp_rebuild.json', reconstruction_residual_ms=meta['residual_ms'],
            frames_received=received, minimum_frames_sent=sent, minimum_frames_dropped=dropped,
            minimum_dropped_fraction=dropped / sent, loss_warn_fraction=.001, loss_error_fraction=.01)
        # A normal EMG step of two includes a received IMU frame and contributes
        # zero loss because the sequence is checked in the original wire order.
        loss_events = np.flatnonzero(missing > 0)
        def loss_event(i):
            return dict(before_raw_index=int(selected[i]), after_raw_index=int(selected[i + 1]),
                        before_unix_s=float(epoch[selected[i]]), minimum_missing_frames=int(missing[i]))
        loss_facts = [loss_event(int(i)) for i in loss_events[:EVENT_LIMIT]]
        out.stats.update(loss_event_count=int(loss_events.size), loss_events=loss_facts,
                         loss_events_truncated=bool(loss_events.size > EVENT_LIMIT),
                         maximum_missing_wire_burst=int(missing.max()) if missing.size else 0,
                         worst_loss_event=loss_event(int(np.argmax(missing))) if dropped else None)
        loss_severity = emg_loss_level(dropped, sent)
        if loss_severity:
            out.findings.append(self.finding(loss_severity,
                '肌电/IMU 共用链路至少丢失 %d/%d 帧（%.3f%%）' % (dropped, sent, dropped * 100 / sent),
                field='raw_frames', subject='shared_wire_SN', observed=dropped / sent,
                threshold=.01 if loss_severity == 'ERROR' else .001,
                detail=dict(loss_is_lower_bound=True, events=loss_facts, event_count=int(loss_events.size))))
        all_em_indices = np.flatnonzero(em)
        em_indices = np.flatnonzero(em & mask)
        if em_indices.size < 2:
            return _unknown(self, out, 'fewer_than_two_EMG_frames_in_recording_window')
        em_clock = (index[em_indices] - index[em_indices[0]]) * period_ns
        em_npz_indices = np.cumsum(em)[em_indices] - 1
        facts = _gaps(em_clock, em_npz_indices, epoch[em_indices], EMG_WARN_NS, EMG_ERROR_NS)
        cumulative_loss = np.r_[0, np.cumsum(steps - 1)]
        for event in facts['events'] + ([facts['worst_event']] if facts['worst_event'] else []):
            before, after = int(all_em_indices[event['before_index']]), int(all_em_indices[event['after_index']])
            event.update(before_raw_index=before, after_raw_index=after,
                         minimum_missing_wire_frames=int(cumulative_loss[after] - cumulative_loss[before]))
        out.stats['sample_gap'] = facts
        _add_gap_finding(self, out, facts, 'emg_timestamps', 'emg')

        unknown = []
        if meta['residual_ms'] > 5.0:
            unknown.append('large_reconstruction_anchor_residual')
        # Host clocks identify ambiguous wrap/latency episodes, never missing
        # samples. A whole buffered batch may arrive late without losing data.
        try:
            em_mono = _integer_array(ctx.arr('emg_monotonic_ns'), 'emg_monotonic_ns', esn.size)
            im_mono = _integer_array(ctx.arr('imu_monotonic_ns'), 'imu_monotonic_ns', isn.size)
            mono = np.empty(raw.shape[0], dtype=np.int64)
            mono[em], mono[im] = em_mono, im_mono
            if np.any(np.diff(mono) < 0):
                raise ValueError('raw_host_monotonic_clock_backstep')
            last_in_batch = np.r_[np.flatnonzero(np.diff(mono) != 0), mono.size - 1]
            host_delta = np.diff(mono[last_in_batch])
            fit_delta = np.diff(index[last_in_batch]) * period_ns
            ambiguous = np.flatnonzero(np.abs(host_delta - fit_delta) > 128 * period_ns)
            out.stats.update(host_receipt_clock_basis='serial_read_monotonic_batches_not_sample_clock',
                ambiguous_batch_interval_count=int(ambiguous.size),
                ambiguous_batch_intervals=[dict(before_raw_index=int(last_in_batch[i]),
                    after_raw_index=int(last_in_batch[i + 1]), host_interval_ns=int(host_delta[i]),
                    sequence_model_interval_ns=int(fit_delta[i])) for i in ambiguous[:EVENT_LIMIT]])
            if ambiguous.size:
                unknown.append('counter_wrap_or_host_batch_latency_ambiguous')
        except (ValueError, TypeError, OverflowError) as exc:
            unknown.append(str(exc))
        out.stats.update(status='error' if out.level == 'ERROR' else 'warn' if out.level == 'WARN' else 'pass',
                         passed=out.level == 'INFO', shared_interleave_verified=True)
        if unknown:
            _unknown(self, out, ';'.join(unknown))
            # Keep known errors while explicitly declining a timing PASS.
            if out.level == 'ERROR':
                out.stats.update(status='error', passed=False, timing_status='unknown')
        return out


def checks_for_stream(name):
    if name in WRISTS:
        return (PiWristFrameGap(),)
    if name in EMGS:
        return (PiEmgContinuity(),)
    return ()
