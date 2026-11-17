"""OmniBCI data contracts and QC; host receipt times are not hardware clocks."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import numpy as np
from embodied_brain_collect.checkers.base import BaseCheck, BaseChecker, CheckOutput
from eeg_settings import RECORDED_LAYOUTS


def recorded_profile(enabled, labels):
    """Use saved metadata for old 7ch and current 8ch recordings; never relabel."""
    if enabled.shape != (8,) or labels is None or labels.shape != (8,):
        return False
    mask = sum((1 << i) for i, active in enumerate(enabled) if active)
    return mask in RECORDED_LAYOUTS and labels.tolist() == RECORDED_LAYOUTS[mask]


def _atomic_eeg_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def finalize_eeg_gate(root, arrays):
    """Resolve provisional SDK batches using the controller's final STOP.

    The STOP timestamp precedes fsync/rename of stop_gate.json. An EEG worker
    can accept a batch during that interval. Keep raw chunks unchanged and
    exclude that suffix only from the standardized, gated data product.
    """
    root = Path(root)
    gates = {name: json.loads((root/(name+'_gate.json')).read_text())
             for name in ('start', 'stop')}
    start = gates['start']['start_monotonic_ns']
    stop = gates['stop']['stop_monotonic_ns']
    if type(start) is not int or type(stop) is not int or not 0 < start < stop:
        raise RuntimeError('eeg: invalid final recording gates')
    n = len(arrays['sample_indices'])
    if np.any(np.diff(arrays['sample_indices'].astype(np.int64)) <= 0):
        raise RuntimeError('eeg: non-increasing raw sample indices')
    for key in ('read_begin_monotonic_ns', 'arrival_monotonic_ns', 'sdk_received_monotonic_ns'):
        if arrays[key].shape != (n,) or arrays[key].dtype.kind not in 'iu':
            raise RuntimeError('eeg: malformed host batch clocks')
    begin = arrays['read_begin_monotonic_ns']
    end = arrays['arrival_monotonic_ns']
    received = arrays['sdk_received_monotonic_ns']
    if np.any(begin < start) or np.any(received < start):
        raise RuntimeError('eeg: pre-START data cannot be repaired as a STOP boundary race')
    if np.any(end < begin) or np.any(end < received) or np.any(np.diff(end.astype(np.int64)) < 0):
        raise RuntimeError('eeg: invalid host read clock order')
    keep = end < stop
    kept = int(keep.sum())
    if kept == 0:
        raise RuntimeError('eeg: no complete SDK batch inside final recording gates')
    if not keep[:kept].all() or keep[kept:].any():
        raise RuntimeError('eeg: STOP exclusions must be a contiguous suffix')
    proof = dict(schema_version=1, policy='whole_sdk_batches_final_controller_gate_v1',
        start_monotonic_ns=start, stop_monotonic_ns=stop,
        gate_sha256={name: hashlib.sha256((root/(name+'_gate.json')).read_bytes()).hexdigest()
                     for name in ('start', 'stop')},
        input_samples=n, kept_samples=kept, excluded_samples=n-kept,
        excluded_batches=len(np.unique(np.stack((begin[~keep], end[~keep]), axis=1), axis=0)),
        max_completion_after_stop_ms=float(max(0, int(end.max())-stop)/1e6),
        original_first_sample_index=int(arrays['sample_indices'][0]),
        original_last_sample_index=int(arrays['sample_indices'][-1]),
        excluded_first_sample_index=int(arrays['sample_indices'][kept]) if kept<n else None,
        excluded_last_sample_index=int(arrays['sample_indices'][-1]) if kept<n else None,
        original_chunks_preserved=True, raw_evidence_directory='raw_chunks',
        timestamp_changes=False)
    return {key: value[keep] for key, value in arrays.items()}, proof


def _record_finalized_eeg(directory, summary, arrays, proof):
    idx = arrays['sample_indices']
    stats = dict(samples=len(idx), first_sample_index=int(idx[0]), last_sample_index=int(idx[-1]),
        invalid_samples=int((~arrays['valid']).sum()),
        missing_samples=int(np.maximum(np.diff(idx.astype(np.int64))-1, 0).sum()),
        samples_before_gate_finalization=proof['input_samples'],
        boundary_excluded_samples=proof['excluded_samples'])
    summary['eeg'].update(stats, gate_finalization='eeg/gate_finalization.json')
    metadata_path = directory/'metadata.json'
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata.setdefault('statistics', {}).update(stats)
        metadata['gate_finalization'] = proof
        _atomic_eeg_json(metadata_path, metadata)
    summary_path = directory/'eeg_summary.json'
    if summary_path.is_file():
        stored = json.loads(summary_path.read_text(encoding='utf-8'))
        stored.update(stats, gate_finalization='eeg/gate_finalization.json')
        _atomic_eeg_json(summary_path, stored)


def standardize_eeg(root, summary, atomic_npz):
    if 'eeg' not in summary: return
    directory = Path(root)/'eeg'
    output = directory/'eeg.npz'
    if output.exists():
        # A retry after interruption between the durable output and chunk
        # archival must recover the same accepted count, never the provisional
        # native count. Historical outputs without this proof are left intact.
        proof_path = directory/'gate_finalization.json'
        if proof_path.is_file():
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            with np.load(output, allow_pickle=False) as stored:
                arrays = {key:stored[key].copy() for key in ('sample_indices','valid')}
            if (len(arrays['sample_indices']) != proof['kept_samples'] or
                summary['eeg']['samples'] not in (proof['input_samples'],proof['kept_samples'])):
                raise RuntimeError('eeg: existing final output count differs from proof/summary')
            _record_finalized_eeg(directory, summary, arrays, proof)
            archive = directory/'raw_chunks'; archive.mkdir(exist_ok=True)
            for chunk in sorted(directory.glob('chunk_*.npz')):
                if (archive/chunk.name).exists():
                    raise RuntimeError('eeg: ambiguous duplicate raw chunk during recovery')
                chunk.rename(archive/chunk.name)
        return
    chunks = sorted(directory.glob('chunk_*.npz'))
    if not chunks: raise RuntimeError('eeg: no raw chunks')
    parts = {}; constants = None
    keys = ('eeg_uv','raw_counts','sequence','valid','sample_indices','status','mode','eeg_filtered_uv',
            'read_begin_monotonic_ns','arrival_monotonic_ns','arrival_unix_ns','sdk_received_monotonic_ns')
    for chunk in chunks:
        with np.load(chunk, allow_pickle=False) as arrays:
            current = {k:arrays[k].copy() for k in ('sample_rate_hz','generation','channel_enabled','channel_labels','filter_preset','timestamp_basis')}
            if constants is None: constants = current
            elif any(not np.array_equal(constants[k], current[k]) for k in constants):
                raise RuntimeError('eeg: chunk metadata changed')
            n = len(arrays['sample_indices'])
            for key in keys:
                if len(arrays[key]) != n: raise RuntimeError('eeg: unequal chunk field lengths')
                parts.setdefault(key, []).append(arrays[key])
    arrays = {k:np.concatenate(v) for k,v in parts.items()}
    n = len(arrays['sample_indices'])
    if n != summary['eeg']['samples']: raise RuntimeError('eeg: merged count differs from capture summary')
    enabled = constants['channel_enabled'].astype(bool)
    if not recorded_profile(enabled, constants['channel_labels']):
        raise RuntimeError('eeg: wrong recorded channel enable mask/layout')
    arrays, proof = finalize_eeg_gate(root, arrays)
    n = len(arrays['sample_indices'])
    proof['source_chunks'] = [dict(name=p.name, bytes=p.stat().st_size,
        sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in chunks]
    _atomic_eeg_json(directory/'gate_finalization.json', proof)
    _record_finalized_eeg(directory, summary, arrays, proof)
    raw_uv = arrays.pop('eeg_uv')
    arrays.update(eeg_data=raw_uv[:,enabled], eeg_raw_uv=raw_uv,
        eeg_filtered_data=arrays.pop('eeg_filtered_uv')[:,enabled],
        eeg_sample_rate=constants['sample_rate_hz'], eeg_n_samples=n,
        eeg_channel_labels=constants['channel_labels'][enabled], **constants)
    # Retain device-relative time plus measured batch receipt clocks. No
    # eeg_timestamps_pc field or fictitious successful clock fit is generated.
    arrays['eeg_sample_time_s'] = arrays['sample_indices'].astype(np.float64)/250.
    atomic_npz(output, **arrays)
    archive = directory/'raw_chunks'; archive.mkdir(exist_ok=True)
    for chunk in chunks: chunk.rename(archive/chunk.name)


@dataclass(frozen=True)
class OmniEegIntegrity(BaseCheck):
    def run(self, ctx):
        out = CheckOutput()
        required = ('eeg_data','raw_counts','valid','sample_indices','arrival_monotonic_ns',
                    'read_begin_monotonic_ns','sdk_received_monotonic_ns','channel_enabled','channel_labels','eeg_sample_rate')
        data = {k:ctx.arr(k) for k in required}
        if any(v is None for v in data.values()):
            out.findings.append(self.finding('ERROR','脑电文件缺少完整性字段')); return out
        n = len(data['sample_indices'])
        enabled = data['channel_enabled'].astype(bool)
        if not recorded_profile(enabled, data['channel_labels']):
            out.findings.append(self.finding('ERROR','脑电通道启用状态或保存的通道名称不匹配')); return out
        if not n or data['eeg_data'].shape != (n,int(enabled.sum())) or data['raw_counts'].shape != (n,8):
            out.findings.append(self.finding('ERROR','脑电样本数量或通道维度错误')); return out
        if float(data['eeg_sample_rate']) != 250:
            out.findings.append(self.finding('ERROR','脑电采样率配置错误'))
        differences = np.diff(data['sample_indices'].astype(np.int64))
        if np.any(differences <= 0):out.findings.append(self.finding('ERROR','脑电样本索引未严格递增'))
        missing = int(np.maximum(differences-1,0).sum())
        invalid = int((~data['valid']).sum())
        if missing:out.findings.append(self.finding('WARN','脑电样本索引有丢失：'+str(missing), observed=missing))
        if invalid:out.findings.append(self.finding('WARN','脑电固件标记无效样本：'+str(invalid), observed=invalid))
        good = data['valid'] & np.isfinite(data['eeg_data']).all(axis=1)
        if not np.isfinite(data['eeg_data']).all():out.findings.append(self.finding('WARN','有效脑电通道中有非有限数值'))
        raw = data['raw_counts'][:,enabled]
        rails = (np.abs(raw.astype(np.int64)) >= 8388607).sum(axis=0)
        if rails.any():out.findings.append(self.finding('WARN','脑电 ADC 达到量程边界，请检查接触与参考电极', detail={'rail_samples':rails.tolist(),'physical_channels':(np.flatnonzero(enabled)+1).tolist()}))
        values = data['eeg_data'][good]
        if len(values):
            std = values.std(axis=0)
            out.stats.update(channel_std_uv=std.tolist(), channel_peak_to_peak_uv=np.ptp(values,axis=0).tolist())
            if np.any(std < 1e-6):out.findings.append(self.finding('WARN','存在无变化的有效脑电通道'))
        else:out.findings.append(self.finding('WARN','没有有效脑电样本'))
        root = ctx.dir.parent
        start = json.loads((root/'start_gate.json').read_text())['start_monotonic_ns']
        stop = json.loads((root/'stop_gate.json').read_text())['stop_monotonic_ns']
        if (np.any(data['read_begin_monotonic_ns'] < start) or
            np.any(data['sdk_received_monotonic_ns'] < start) or
            np.any(data['arrival_monotonic_ns'] >= stop)):
            out.findings.append(self.finding('ERROR','脑电主机读批次超出统一录制门限'))
        finalization = ctx.dir/'gate_finalization.json'
        if finalization.is_file():
            try:
                proof = json.loads(finalization.read_text(encoding='utf-8'))
                if (proof['schema_version'] != 1 or
                    type(proof['excluded_samples']) is not int or proof['excluded_samples'] < 0 or
                    proof['start_monotonic_ns'] != start or proof['stop_monotonic_ns'] != stop or
                    proof['kept_samples'] != n or proof['input_samples'] != n+proof['excluded_samples'] or
                    any(proof['gate_sha256'][name] != hashlib.sha256((root/(name+'_gate.json')).read_bytes()).hexdigest()
                        for name in ('start', 'stop'))):
                    raise ValueError('finalization proof differs from recorded gates/counts')
                if not proof['source_chunks']:
                    raise ValueError('missing raw chunk manifest')
                for source in proof['source_chunks']:
                    name = source['name']
                    if not isinstance(name,str) or Path(name).name != name or not name.startswith('chunk_') or not name.endswith('.npz'):
                        raise ValueError('unsafe raw evidence path')
                    path = ctx.dir/'raw_chunks'/name
                    if path.is_symlink() or not path.is_file() or path.stat().st_size != source['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
                        raise ValueError('original raw chunk missing or changed: '+name)
                out.stats.update(boundary_excluded_samples=proof['excluded_samples'],
                    samples_before_gate_finalization=proof['input_samples'])
                if proof['excluded_samples']:
                    out.findings.append(self.finding('INFO','已按最终停止门限排除末尾跨界脑电批次；原始批次完整保留',
                        observed=proof['excluded_samples'], detail={'evidence':'gate_finalization.json',
                            'raw_evidence':'raw_chunks', 'batches':proof['excluded_batches']}))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                out.findings.append(self.finding('ERROR','脑电最终门限处理记录无效：'+str(exc)))
        duration = max(0., (stop-start)/1e9)
        if duration > 2 and n < duration*250*.9:
            out.findings.append(self.finding('WARN','脑电样本数低于录制时长预期的 90%', observed=n, threshold=duration*225))
        out.stats.update(samples=n, missing_samples=missing, invalid_samples=invalid,
            adc_rail_samples=rails.tolist(), sample_rate_hz=250, duration_seconds=duration,
            sample_span_seconds=float((data['sample_indices'][-1]-data['sample_indices'][0]+1)/250),
            time_basis='sample indices and measured host read clocks; hardware alignment unmeasured')
        out.findings.append(self.finding('INFO','脑电使用主机软件门限，未测量与相机/肌电的硬件同步误差'))
        return out


@dataclass(frozen=True)
class OmniEegImpedance(BaseCheck):
    """Verify the pre-recording evidence actually committed for this segment."""

    def run(self, ctx):
        from impedance_record import validate_start_binding
        out = CheckOutput()
        root = Path(ctx.dir).parent
        try:
            metadata = json.loads((Path(ctx.dir)/'metadata.json').read_text(encoding='utf-8'))
            gate = json.loads((root/'start_gate.json').read_text(encoding='utf-8'))
            marker = gate.get('eeg_impedance')
            skip_device = marker.get('device_id') if isinstance(marker,dict) else None
            skip_marker = dict(version=1,device_id=skip_device,status='not_measured',reason='operator_requested_skip')
            expected_prefix = {'cp_0013':'L2-001-', 'cp_0017':'L2-017-', 'cp_0018':'L2-018-', 'cp_0023':'L2-023-'}
            if skip_device in expected_prefix and marker == skip_marker:
                if not root.name.startswith(expected_prefix[skip_device]):raise ValueError('跳过阻抗记录与设备身份不匹配')
                readiness = gate.get('required_device_readiness', {}).get('eeg', {})
                if ('eeg' not in gate.get('required_devices', []) or readiness.get('ready') is not True
                        or readiness.get('fresh') is not True or not isinstance(readiness.get('seen'),int)
                        or isinstance(readiness.get('seen'),bool) or readiness['seen'] <= 0):
                    raise ValueError('跳过阻抗的记录缺少脑电在线与新鲜数据证明')
                out.stats.update(impedance_required=False, impedance_measured=False,
                                 impedance_skip_reason='operator_requested_skip')
                out.findings.append(self.finding('WARN','按要求跳过采前阻抗测量；已检查脑电设备在线及数据新鲜度',
                    field='start_gate.json',detail={'device_id':skip_device,'impedance_measured':False}))
                return out
            proof = validate_start_binding(root, gate, expected_eeg_config=metadata['config'])
            preflight = json.loads((Path(ctx.dir)/'impedance_preflight.json').read_text(encoding='utf-8'))
            if metadata['frontend_config'] != preflight['restored_frontend']:
                raise ValueError('recorded EEG frontend differs from impedance recovery configuration')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            out.findings.append(self.finding('ERROR', '本段采前脑电阻抗落盘证据无效：'+str(exc),
                field='impedance_preflight.json', detail={'required':True}))
            return out
        enabled = [c for c in preflight['channels'] if c['enabled']]
        missing = [dict(channel=c['channel'], label=c['label'], reason=c['reason'])
                   for c in enabled if c['value_kohm'] is None]
        out.stats.update(impedance_required=True, impedance_evidence_verified=True,
            impedance_measurement_epoch=proof['measurement_epoch'],
            impedance_preflight_sha256=proof['preflight_sha256'], impedance_units='kOhm',
            impedance_method=preflight['method'], impedance_enabled_channels=len(enabled),
            impedance_missing_channels=missing,
            impedance_values_kohm={c['label']:c['value_kohm'] for c in enabled},
            impedance_review_to_start_seconds=(gate['start_monotonic_ns']-
                proof['reviewed_monotonic_ns'])/1e9,
            impedance_quality_policy=preflight.get('impedance_acceptance_policy', 'strict'),
            impedance_quality=preflight.get('quality'),
            impedance_quality_passed=(preflight.get('quality') or {}).get('passed'))
        if missing:
            out.findings.append(self.finding('WARN', '部分启用脑电通道的阻抗无有效读数；已保留原因及人工确认',
                field='impedance_preflight.json', detail={'channels':missing}))
        if isinstance(preflight.get('quality'), dict) and preflight['quality'].get('passed') is False:
            out.findings.append(self.finding('WARN', '脑电阻抗质量未通过；已保留真实测量结果',
                field='impedance_preflight.json', detail={'quality':preflight['quality']}))
        out.findings.append(self.finding('INFO', '已核对本段开始前保存的阻抗检测、人工确认与前端恢复证据',
            field='impedance_used.json', detail={'measurement_epoch':proof['measurement_epoch'],
                'method':preflight['method'], 'units':'kOhm'}))
        return out


class PiEegChecker(BaseChecker):
    name = 'omnibci_eeg'
    matches = ('eeg',)
    checks = [OmniEegIntegrity(), OmniEegImpedance()]
