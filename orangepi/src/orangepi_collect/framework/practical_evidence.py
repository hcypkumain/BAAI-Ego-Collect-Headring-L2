"""Facts for practical QC policy; reuse original arrays and video decode cache.

No thresholds, findings, filtering, timestamp repair, or hardware operations.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np

from embodied_brain_collect.checkers.base import BaseCheck, CheckOutput
from embodied_brain_collect.checkers.checks import FrameCountMatch


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _window(value):
    if (isinstance(value, dict) and _number(value.get('t0')) and _number(value.get('t1'))
            and value['t1'] >= value['t0']):
        return {'t0':float(value['t0']), 't1':float(value['t1'])}
    return None


def _facts(values, window, *, nanoseconds=False, batch_clock=False):
    array = np.asarray(values)
    if array.ndim != 1 or array.dtype.kind not in 'ifu':
        return {'available':False, 'reason':'timestamp_array_is_not_one_dimensional_numeric'}
    count = int(array.size)
    finite_count=int(np.isfinite(array).sum())
    result = dict(available=bool(finite_count), n=count, n_nonfinite=count-finite_count,
                  first=None, last=None, head_margin_s=None, tail_margin_s=None,
                  max_actual_interval_s=None)
    if not count:
        result['reason']='empty_timestamp_array'
        return result
    if not finite_count:
        result['reason']='no_finite_timestamp_values'
        return result
    if nanoseconds and array.dtype.kind not in 'iu':
        result.update(available=False,reason='host_nanosecond_clock_is_not_integer')
        return result
    if array.dtype.kind in 'iu':array=array.astype(np.int64,copy=False)
    # Do not sort, renumber, bridge NaNs, or replace the original timestamps.
    delta = np.diff(array.astype(np.int64) if array.dtype.kind in 'iu' else array)
    scale = 1e-9 if nanoseconds else 1.0
    result.update(n_backsteps=int(np.sum(delta < 0)), n_duplicates=int(np.sum(delta == 0)))
    if batch_clock:
        keep = np.r_[True, delta != 0]
        array = array[keep]
        delta = np.diff(array)
        result['n_clock_batches']=int(array.size)
        result['interval_basis']='distinct_consecutive_host_batch_clocks'
    else:
        result['interval_basis']='adjacent_original_recorded_timestamps'
    if np.isfinite(array[0]):result['first']=float(array[0])*scale
    if np.isfinite(array[-1]):result['last']=float(array[-1])*scale
    finite_delta = delta[np.isfinite(delta)]
    if finite_delta.size:result['max_actual_interval_s']=float(finite_delta.max())*scale
    if window is not None:
        if np.isfinite(array[0]):result['head_margin_s']=float(array[0]-window['t0'])*scale
        if np.isfinite(array[-1]):result['tail_margin_s']=float(window['t1']-array[-1])*scale
    return result


def _gate_clock(root, filename, key):
    try:
        with (Path(root)/filename).open('rb') as handle:
            payload=handle.read(65537)
        if len(payload)>65536:return None
        value=json.loads(payload.decode('utf-8'))
        result=value.get(key) if isinstance(value,dict) else None
        return result if type(result) is int and result>0 else None
    except (OSError,ValueError,UnicodeError):
        return None


@dataclass(frozen=True)
class PiTimingEvidence(BaseCheck):
    """Per-series actual intervals from the recorded timestamps."""

    def run(self, ctx):
        out=CheckOutput()
        stats=dict(schema_version=1,available=False,series={},window=_window(ctx.window),
                   clock_basis='registered recorded timestamp series; no new clock alignment')
        out.stats.update(stats)
        if Path(ctx.dir).name.startswith('marker') or str(getattr(ctx,'stream','')).startswith('marker'):
            out.stats['reason']='sparse_event_stream_not_a_continuous_sensor'
            return out
        labels=list(ctx.series_labels)
        for label in labels:
            series=ctx.series(label)
            if series is None:
                out.stats['series'][label]={'available':False,'reason':'registered_series_missing'}
                continue
            facts=_facts(series.t,out.stats['window'])
            facts['clock_basis']='original_registered_timestamp_series_seconds'
            facts['window']=out.stats['window']
            out.stats['series'][label]=facts
        if labels:
            out.stats['available']=any(v.get('available') for v in out.stats['series'].values())
            if not out.stats['available']:out.stats['reason']='no_usable_registered_series'
            return out
        out.stats['reason']='no_registered_continuous_series'
        return out


def _black_runs(mask):
    """Global count/max in one bounded scan; never truncate before the max."""
    count=maximum=current=black=0
    for value in mask:
        if value:
            black+=1
            current+=1
            if current==1:count+=1
            if current>maximum:maximum=current
        else:
            current=0
    return black,count,maximum


@dataclass(frozen=True)
class PiVideoEvidence(BaseCheck):
    """Black-screen run facts from the original cached decoder, without verdicts."""
    video: str = 'frames.mp4'
    lum_thr: float = 8.0

    def run(self, ctx):
        out=CheckOutput()
        out.stats.update(schema_version=1,available=False,file=self.video,coverage='unavailable')
        if not _number(self.lum_thr) or not 0<=self.lum_thr<=255:
            out.stats['reason']='invalid_luminance_threshold'
            return out
        if not (Path(ctx.dir)/self.video).is_file():
            out.stats['reason']='video_missing'
            return out
        try:
            dec=FrameCountMatch(video=self.video).decode(ctx)
        except Exception as exc:
            out.stats.update(reason='original_decoder_evidence_unavailable',detail=str(exc)[:240])
            return out
        if not dec.opened:
            out.stats['reason']='video_decoder_did_not_open'
            return out
        full_mask=getattr(dec,'black_mask',None)
        full_lums=getattr(dec,'black_lums',None)
        if full_mask is not None:
            if getattr(dec,'black_lum_thr',None)!=self.lum_thr:
                out.stats['reason']='full_frame_black_threshold_mismatch'
                return out
            mask=np.asarray(full_mask)
            rate=getattr(dec,'black_rate_hz',None)
            coverage='full_frame_mask'
            if mask.ndim!=1 or mask.dtype!=np.bool_:
                out.stats['reason']='invalid_full_frame_black_mask'
                return out
        else:
            lums=full_lums if full_lums is not None else getattr(dec,'lums',None)
            rate=(getattr(dec,'black_rate_hz',None) if full_lums is not None else getattr(dec,'rate_hz',None))
            coverage='full_frame_luminance' if full_lums is not None else 'sampled'
            if lums is None:
                out.stats['reason']='luminance_evidence_missing'
                return out
            lums=np.asarray(lums)
            if lums.ndim!=1 or lums.dtype.kind not in 'ifu' or not np.isfinite(lums).all():
                out.stats['reason']='invalid_luminance_evidence'
                return out
            mask=lums<self.lum_thr
        if not _number(rate) or rate<=0 or not mask.size:
            out.stats['reason']='empty_black_evidence_or_invalid_sampling_rate'
            return out
        black,n_runs,max_run=_black_runs(mask)
        expected=getattr(dec,'n_window',None)
        if expected is None:expected=getattr(dec,'n_frames',None)
        out.stats.update(available=True,file=dec.file,coverage=coverage,
            coverage_complete=bool(coverage!='sampled' and expected==int(mask.size)),
            n_checked_frames=int(mask.size),sampling_rate_hz=float(rate),
            max_black_run_s=max_run/float(rate),black_duration_s=black/float(rate),
            black_fraction=black/int(mask.size),black_segment_count=n_runs,
            checked_duration_s=int(mask.size)/float(rate),
            expected_decoded_frames=int(expected) if expected is not None else None,
            luminance_threshold=float(self.lum_thr),
            duration_basis='nominal_checked_frame_intervals; source_timestamp_gaps_are_separate',
            sampled_interval_s=(1/float(rate) if coverage=='sampled' else None))
        return out
