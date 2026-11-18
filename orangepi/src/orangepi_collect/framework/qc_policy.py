"""Pure Pi QC severity policy. No files, arrays, clocks or hardware are repaired.

load_policy() is the only optional file read. apply_policy() deep-copies the
original report and keeps its SessionReport/StreamReport/Finding interfaces.
"""
import copy
import dataclasses
import json
import math
from pathlib import Path
import sys

DEFAULT_POLICY = dict(schema_version=1, preset='practical-v1', mode='practical',
    actual_gap_error_s=3.0, edge_warn_s=0.5, black_run_error_s=5.0,
    black_fraction_error=0.95)


class PolicyError(ValueError):
    pass


class PolicyConfig(dict):
    def __init__(self, value, source, path=None):
        super().__init__(value)
        self.source = source
        self.path = path


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _config(value, source='explicit_config', path=None):
    if not isinstance(value, dict) or set(value)-set(DEFAULT_POLICY):
        raise PolicyError('QC policy must be an object with known fields')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise PolicyError('QC policy schema_version must be 1')
    merged = dict(DEFAULT_POLICY, **value)
    if merged['preset'] != 'practical-v1' or merged['mode'] not in ('practical','strict'):
        raise PolicyError('Unknown QC policy preset or mode')
    for key in ('actual_gap_error_s','edge_warn_s','black_run_error_s','black_fraction_error'):
        if not _number(merged[key]) or merged[key] <= 0:
            raise PolicyError('QC policy requires a finite positive '+key)
    if (merged['edge_warn_s'] >= merged['actual_gap_error_s']
            or merged['black_fraction_error'] > 1):
        raise PolicyError('QC policy thresholds are inconsistent')
    return PolicyConfig(merged, source, path)


def load_policy(path=None):
    """Read standalone JSON. Missing files explicitly use the built-in policy.

    A directory argument resolves to configs/qc_policy.json. Invalid JSON,
    schema or finite thresholds raise ValueError, never a silent fallback.
    """
    if path is None:
        return _config(DEFAULT_POLICY, 'default_builtin')
    target = Path(path)
    if target.is_dir():
        target = target/'configs/qc_policy.json'
    if not target.exists():
        return _config(DEFAULT_POLICY, 'default_missing_file', str(target))
    with target.open('rb') as handle:
        payload = handle.read(65537)
    if len(payload) > 65536:
        raise PolicyError('QC policy JSON is too large')
    try:
        value = json.loads(payload.decode('utf-8'),
            parse_constant=lambda token: (_ for _ in ()).throw(PolicyError('Non-finite JSON '+token)))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError('Cannot decode QC policy JSON') from exc
    return _config(value, 'json_file', str(target))


def _usable_timing(value):
    if (not isinstance(value, dict) or value.get('available') is not True
            or type(value.get('n')) is not int or value['n'] < 2
            or value.get('n_nonfinite') != 0 or value.get('n_backsteps') != 0
            or not _number(value.get('first')) or not _number(value.get('last'))
            or value['last'] < value['first']):
        return False
    interval = value.get('max_actual_interval_s')
    if interval is not None and (not _number(interval) or interval < 0):
        return False
    window = value.get('window')
    for key in ('head_margin_s','tail_margin_s'):
        margin = value.get(key)
        if margin is not None and (not _number(margin) or margin < -1e-6):
            return False
        if window is not None and margin is None:
            return False
    # A batch stream may collapse to a single distinct receipt clock. Its zero
    # interval is not proof of dropout; window margins still describe coverage.
    return True


def _timing_series(stream):
    stats = stream.stats.get('PiTimingEvidence', {})
    if (not isinstance(stats, dict) or stats.get('schema_version') != 1
            or stats.get('available') is not True):
        return {}
    series = stats.get('series')
    return series if isinstance(series, dict) else {}


def _target_timing(stream, finding):
    series = _timing_series(stream)
    label = finding.subject
    if label in series:
        value = series[label]
    elif not label and len(series) == 1:
        value = next(iter(series.values()))
    else:
        return None
    return value if _usable_timing(value) else None


def _maximum_timing(value):
    return max([0.] + [value[key] for key in
        ('max_actual_interval_s','head_margin_s','tail_margin_s') if _number(value.get(key))])


def _recorded_wrist_gap_supersedes(stream, name, finding):
    """Only replace legacy *interior* gaps, never edges or structural errors."""
    facts=stream.stats.get('PiWristFrameGap',{})
    return (name in ('cam_left_wrist','cam_right_wrist')
            and finding.check=='TimestampGap' and finding.subject=='frames'
            and isinstance(finding.detail,dict)
            and type(finding.detail.get('n_segments')) is int and finding.detail['n_segments']>0
            and isinstance(facts,dict) and facts.get('schema_version')==1
            and facts.get('status') in ('pass','warn','error')
            and facts.get('timestamp_field')=='source_pts_ns'
            and facts.get('warn_threshold_ns')==100000000
            and facts.get('error_threshold_ns')==150000000)


def _verified_emg_interleave(stream, name, finding):
    facts=stream.stats.get('PiEmgContinuity',{})
    return (name in ('emg_left','emg_right') and finding.check=='IntervalJitter'
            and finding.subject=='emg' and isinstance(facts,dict)
            and facts.get('schema_version')==1 and facts.get('status')=='pass'
            and facts.get('shared_interleave_verified') is True
            and facts.get('minimum_frames_dropped')==0
            and isinstance(facts.get('sample_gap'),dict)
            and facts['sample_gap'].get('exceeds_warn_count')==0)


def _full_video(stream):
    value = stream.stats.get('PiVideoEvidence', {})
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('available') is not True
            or value.get('coverage_complete') is not True
            or value.get('coverage') not in ('full_frame_mask','full_frame_luminance')
            or type(value.get('n_checked_frames')) is not int or value['n_checked_frames'] <= 0
            or value.get('expected_decoded_frames') != value['n_checked_frames']):
        return None
    for key in ('sampling_rate_hz','checked_duration_s'):
        if not _number(value.get(key)) or value[key] <= 0:
            return None
    for key in ('max_black_run_s','black_duration_s','black_fraction'):
        if not _number(value.get(key)) or value[key] < 0:
            return None
    duration = value['checked_duration_s']
    if (value['black_fraction'] > 1 or value['max_black_run_s'] > duration+1e-6
            or value['black_duration_s'] > duration+1e-6
            or value['max_black_run_s'] > value['black_duration_s']+1e-6):
        return None
    return value


def _black_hard(video, config):
    return (video['max_black_run_s'] >= config['black_run_error_s']
            or video['black_fraction'] >= config['black_fraction_error'])


def _replace_level(finding, level, reason):
    detail = copy.deepcopy(finding.detail)
    detail['pi_qc_policy'] = dict(raw_level=finding.level, effective_level=level,
        preset='practical-v1', reason=reason)
    return dataclasses.replace(finding, level=level, detail=detail)


def _finding_class(report):
    module = sys.modules.get(type(report).__module__)
    candidate = getattr(module, 'Finding', None)
    if candidate is not None:
        return candidate
    for item in [report] + list(report.streams.values()):
        if item.findings:
            return type(item.findings[0])
    from embodied_brain_collect.checkers.base import Finding
    return Finding


def apply_policy(report, config=None,preserve_streams=None):
    """Return (effective_report, audit), without mutating the original report.

    Only positively verified timing/full-frame evidence can lower raw ERROR.
    Unknown ERROR and required impedance/format/marker findings remain ERROR.
    Full-frame black or actual long no-data intervals may raise raw WARN/INFO.
    """
    if config is None:
        config = load_policy()
    else:
        config = _config(config, getattr(config,'source','explicit_config'), getattr(config,'path',None))
    effective = copy.deepcopy(report)
    audit = dict(schema_version=1, preset=config['preset'], mode=config['mode'],
        config_source=config.source, config_path=config.path,
        thresholds={k:v for k,v in config.items() if k not in ('schema_version','preset','mode')},
        raw_level=report.level, effective_level=report.level, changes=[], decisions=[], blockers=[], advisories=[], evidence={})
    audit['capture_integrity']=dict(schema_version=1, policy='recorded-stream-v1',
        comparison='strictly_greater_than', wrist_warn_ns=100000000,wrist_error_ns=150000000,
        board_imu_rule_set='original-framework-v1',
        emg_rule_set='emg-gap-150ms-v1',emg_timestamp_gap_error_s=.15,
        emg_wire_loss_warn_fraction=.001,emg_wire_loss_error_fraction=None,
        protected_checks=['PiWristFrameGap'])
    if config['mode'] == 'strict':
        audit['reason'] = 'strict mode preserves the original report and severities'
    else:
        finding_type = _finding_class(report)
        for name, stream in effective.streams.items():
            if name in ('emg_left','emg_right','board_imu') or name in (preserve_streams or ()):
                audit['evidence'][name]=dict(rule_set='verified-Pi-local-result' if name in (preserve_streams or ()) else 'original-framework-v1')
                for index,finding in enumerate(stream.findings):
                    audit['decisions'].append(dict(stream=name,finding_index=index,check=finding.check,
                        raw_level=finding.level,effective_level=finding.level,reason=('verified_Pi_local_QC_preserved' if name in (preserve_streams or ()) else 'original_IMU_framework_severity_preserved' if name=='board_imu' else 'original_EMG_framework_severity_preserved')))
                continue
            video = _full_video(stream)
            timing = _timing_series(stream)
            audit['evidence'][name] = dict(usable_timing_series=[key for key,value in timing.items() if _usable_timing(value)],
                video_coverage=stream.stats.get('PiVideoEvidence',{}).get('coverage','unavailable'),
                video_full_frame_verified=video is not None)
            for index, finding in enumerate(stream.findings):
                raw = finding.level; level = raw; reason = None
                facts = _target_timing(stream, finding)
                if _recorded_wrist_gap_supersedes(stream,name,finding):
                    level='INFO';reason='legacy_interior_gap_superseded_by_exact_recorded_wrist_100_150ms_check'
                elif _verified_emg_interleave(stream,name,finding) and raw=='WARN':
                    level='INFO';reason='verified_shared_counter_EMG_IMU_interleave_is_not_sample_clock_jitter'
                elif finding.check == 'TimestampGap' and raw == 'ERROR':
                    if facts is not None and _maximum_timing(facts) < config['actual_gap_error_s']:
                        level='WARN';reason='short_actual_gap_is_advisory; merged gap duration is not an actual interval'
                    else:
                        reason='raw_gap_ERROR_retained; timing_unavailable_or_actual_interval_reaches_hard_threshold'
                elif (finding.check == 'TimestampSanity' and raw == 'ERROR' and name.startswith('emg')
                        and facts is not None and isinstance(finding.detail,dict)
                        and _number(finding.detail.get('fraction')) and 0 < finding.detail['fraction'] <= 1
                        and type(facts.get('n_duplicates')) is int and facts['n_duplicates'] > 0):
                    level='WARN';reason='duplicate_EMG_arrival_timestamps_with_usable_recorded_timing_are_advisory'
                elif finding.check == 'BlackFrame' and video is not None:
                    if _black_hard(video,config):
                        level='ERROR';reason='extreme_black_screen_verified_on_complete_original_frame_coverage'
                    elif raw == 'ERROR':
                        level='WARN';reason='short_fragmented_black_screen_below_practical_hard_thresholds'
                # Freeze remains its original WARN. No physiology threshold,
                # signal correction, channel dropping or file repair is added.
                if level != raw:
                    stream.findings[index] = _replace_level(finding, level, reason)
                    audit['changes'].append(dict(stream=name,finding_index=index,check=finding.check,
                        raw_level=raw,effective_level=level,reason=reason))
                audit['decisions'].append(dict(stream=name,finding_index=index,check=finding.check,
                    raw_level=raw,effective_level=level,reason=reason or 'original_severity_retained'))
            for label, facts in timing.items():
                if not _usable_timing(facts):
                    continue
                maximum = _maximum_timing(facts)
                hard = maximum >= config['actual_gap_error_s']
                edges = max([0.] + [facts[k] for k in ('head_margin_s','tail_margin_s') if _number(facts.get(k))])
                if not hard and edges < config['edge_warn_s']:
                    continue
                level = 'ERROR' if hard else 'WARN'
                reason = 'actual_no_data_interval_or_edge_reaches_hard_threshold' if hard else 'recording_edge_margin_is_advisory'
                observed = maximum if hard else edges
                index = len(stream.findings)
                stream.findings.append(finding_type(level,
                    '实用质检：'+str(label)+(' 存在长段无数据或录制边缘缺失' if hard else ' 录制边缘有短暂缺失'),
                    check='PiPracticalTiming',subject=label,
                    observed=observed,threshold=config['actual_gap_error_s'] if hard else config['edge_warn_s'],
                    detail=dict(reason=reason,clock_basis=facts.get('clock_basis'),
                        interval_basis=facts.get('interval_basis'),max_actual_interval_s=facts.get('max_actual_interval_s'),
                        head_margin_s=facts.get('head_margin_s'),tail_margin_s=facts.get('tail_margin_s'))))
                audit['changes'].append(dict(stream=name,finding_index=index,check='PiPracticalTiming',
                    raw_level=None,effective_level=level,reason=reason))
            if (video is not None and _black_hard(video,config)
                    and not any(f.check=='BlackFrame' and f.level=='ERROR' for f in stream.findings)):
                index=len(stream.findings)
                criterion=('max_black_run_s' if video['max_black_run_s']>=config['black_run_error_s']
                           else 'black_fraction')
                stream.findings.append(finding_type('ERROR','实用质检：完整帧证据显示极端长黑屏或近乎全黑',
                    check='PiPracticalBlackScreen',field=video.get('file','frames.mp4'),
                    observed=video[criterion],threshold=(config['black_run_error_s']
                        if criterion=='max_black_run_s' else config['black_fraction_error']),
                    detail=dict(coverage=video['coverage'],max_black_run_s=video['max_black_run_s'],
                        black_fraction=video['black_fraction'],black_fraction_error=config['black_fraction_error'],
                        criterion=criterion)))
                audit['changes'].append(dict(stream=name,finding_index=index,check='PiPracticalBlackScreen',
                    raw_level=None,effective_level='ERROR',reason='extreme_black_screen_verified_on_complete_original_frame_coverage'))
    audit['effective_level']=effective.level
    for name, item in [('session',effective)]+list(effective.streams.items()):
        for index, finding in enumerate(item.findings):
            if finding.level not in ('WARN','ERROR'):
                continue
            target = audit['blockers'] if finding.level=='ERROR' else audit['advisories']
            target.append(dict(stream=name,finding_index=index,check=finding.check,level=finding.level,
                field=finding.field,subject=finding.subject,message=finding.message))
    audit['accepted']=effective.level != 'ERROR'
    return effective,audit
