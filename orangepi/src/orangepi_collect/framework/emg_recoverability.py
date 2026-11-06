"""Verified sparse-frame recovery for completed EMG recordings only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import numpy as np

HARD_GAP_S=.1
MAX_RECEIPT_GAP_S=3.0
MAX_LOSS_FRACTION=.01
MAX_REPAIR_FRACTION=.001
MAX_RESIDUAL_MS=5.0

def plan(raw,arrival_ns,window=None,rebuild_fn=None):
    if rebuild_fn is None:
        from embodied_brain_collect.recorders.emg.timestamp_rebuild import rebuild as rebuild_fn
    raw=np.asarray(raw);arrival_ns=np.asarray(arrival_ns)
    if raw.ndim!=2 or raw.shape[1]!=29 or len(raw)<10 or raw.dtype.kind not in 'iu':raise ValueError('invalid_raw_frame_array')
    if arrival_ns.shape!=(len(raw),) or not np.all(np.isfinite(arrival_ns)):raise ValueError('invalid_arrival_array')
    if np.any(raw<0) or np.any(raw>255) or not np.all(raw[:,:3]==0xd2) or not np.all(np.isin(raw[:,3],[0xaa,0xbb])):raise ValueError('unrecoverable_wire_header')
    receipt=np.diff(arrival_ns.astype(np.int64))
    if np.any(receipt<0):raise ValueError('receipt_clock_backwards')
    if receipt.max()/1e9>=MAX_RECEIPT_GAP_S:raise ValueError('receipt_gap_reaches_3s_cannot_verify_full_counter_wraps')
    sn=raw[:,4].astype(np.int64);steps=np.diff(sn)%256
    candidates=np.flatnonzero((steps[:-1]!=1)&(steps[1:]!=1))+1
    removed=[];header_offsets={}
    for i in candidates:
        # Two observed short-frame layouts. An embedded header alone is never
        # sufficient: require adjacent counter continuity and a false wrap.
        frame=raw[i];offsets=[]
        for offset in (7,24):
            if (np.all(frame[offset:offset+3]==0xd2)
                and int(frame[offset+3]) in (0xaa,0xbb)
                and int(frame[offset+4])==(int(sn[i-1])+2)%256
                and int(sn[i+1])==(int(frame[offset+4])+1)%256
                and int(steps[i-1])+int(steps[i])>=256):offsets.append(offset)
        if len(offsets)>1:raise ValueError('ambiguous_embedded_frame_header')
        if offsets:removed.append(int(i));header_offsets[str(int(i))]=offsets[0]
    if len(removed)>max(1,int(len(raw)*MAX_REPAIR_FRACTION)):raise ValueError('too_many_malformed_frames')
    keep=np.ones(len(raw),dtype=bool);keep[removed]=False
    clean=raw[keep];em=clean[:,3]==0xaa;ar=arrival_ns[keep]/1e9
    if em.sum()<10 or (~em).sum()<2:raise ValueError('insufficient_EMG_IMU_data')
    shared_steps=np.diff(clean[:,4].astype(np.int64))%256
    if np.any(shared_steps==0):raise ValueError('shared_sequence_repeat_or_full_wrap_ambiguous')
    fitted=rebuild_fn(ar[em],clean[em,4].astype(np.int64),ar[~em],clean[~em,4].astype(np.int64))
    if not fitted.ok:raise ValueError('reconstruction_refused:'+fitted.note)
    epoch=np.empty(len(clean));epoch[em]=fitted.emg_timestamps;epoch[~em]=fitted.imu_timestamps
    if np.any(np.diff(epoch)<=0):raise ValueError('shared_timeline_remains_nonincreasing')
    if not np.isfinite(fitted.residual_ms) or fitted.residual_ms>MAX_RESIDUAL_MS:raise ValueError('reconstruction_residual_too_large')
    index=np.r_[0,np.cumsum(shared_steps)]
    period_ns=round(fitted.period*1e9)
    residual=np.abs((epoch-epoch[0])*1e9-index*period_ns)
    if not 10000<=period_ns<=20000000 or residual.max()>5000:raise ValueError('shared_sequence_fit_not_verified')
    mask=np.ones(len(epoch),dtype=bool)
    if window is not None:
        t0,t1=float(window['t0']),float(window['t1'])
        if not np.isfinite(t0+t1) or t1<t0:raise ValueError('invalid_recording_window')
        mask=(epoch>=t0)&(epoch<=t1)
        if mask.sum()<10:raise ValueError('recording_window_has_no_usable_data')
        selected=epoch[mask]
        if max(selected[0]-t0,t1-selected[-1])>HARD_GAP_S:raise ValueError('recording_edge_reaches_hard_gap')
    chosen=index[mask]
    missing=int((np.diff(chosen)-1).sum());sent=int(chosen[-1]-chosen[0]+1)
    fraction=missing/sent
    em_index=index[em&mask]
    if len(em_index)<10:raise ValueError('too_few_EMG_samples_in_window')
    maximum=float(np.diff(em_index).max()*period_ns/1e9)
    if maximum>HARD_GAP_S:raise ValueError('continuous_missing_data_reaches_hard_gap')
    if fraction>MAX_LOSS_FRACTION:raise ValueError('wire_loss_exceeds_1_percent')
    info=dict(schema_version=1,verified=True,removed_raw_indices=removed,
        removed_frames=len(removed),splice_header_offsets=header_offsets,original_raw_frames=len(raw),remaining_raw_frames=len(clean),
        maximum_EMG_gap_s=maximum,minimum_wire_frames_missing=missing,minimum_wire_loss_fraction=fraction,
        reconstruction_residual_ms=float(fitted.residual_ms),joint_timeline_strictly_increasing=True,
        hard_gap_s=HARD_GAP_S,gap_comparison="strictly_greater_than",hard_loss_fraction=MAX_LOSS_FRACTION,
        raw_clock_basis='shared_counter_fitted_to_host_receipt_not_hardware_sync')
    return info,keep,fitted

def _digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

def _atomic_json(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.recover-tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def recover_stream(directory,window=None):
    directory=Path(directory);source=directory/(directory.name+'.npz')
    if source.is_symlink() or not source.is_file():raise ValueError('source_npz_is_not_regular')
    before=source.stat();signature=(before.st_ino,before.st_size,before.st_mtime_ns)
    with np.load(source,allow_pickle=False) as z:arrays={k:z[k] for k in z.files}
    if not {'raw_frames','raw_arrival_unix_ns','emg_sn','imu_sn'}<=set(arrays):raise ValueError('source_wire_evidence_missing')
    raw=arrays['raw_frames'];em=raw[:,3]==0xaa
    if not np.array_equal(raw[em,4],arrays['emg_sn']) or not np.array_equal(raw[~em,4],arrays['imu_sn']):raise ValueError('source_sequence_array_mismatch')
    info,keep,fitted=plan(raw,arrays['raw_arrival_unix_ns'],window)
    info.update(stream=directory.name,source=str(source),applied=False)
    if not info['removed_frames']:return info
    native=directory.parent/'native_capture_summary.json'
    if native.is_file():
        d=json.loads(native.read_text());
        if d.get('status')!='success' or d.get('errors'):raise ValueError('native_capture_was_not_successfully_closed')
    # Keep every original source byte, including the failed reconstruction.
    backup=directory/'recovery_originals'/str(time.time_ns());backup.mkdir(parents=True)
    source_sha=_digest(source);meta=directory/'timestamp_rebuild.json'
    shutil.copy2(source,backup/source.name)
    if meta.exists():shutil.copy2(meta,backup/meta.name)
    tmp=directory/(source.name+'.recover-tmp')
    original_em=int(em.sum());original_im=int((~em).sum())
    for k,v in list(arrays.items()):
        if k.startswith('emg_') and v.ndim and len(v)==original_em:arrays[k]=v[keep[em]]
        elif k.startswith('imu_') and v.ndim and len(v)==original_im:arrays[k]=v[keep[~em]]
        elif k.startswith('raw_') and v.ndim and len(v)==len(raw):arrays[k]=v[keep]
    arrays['emg_timestamps']=fitted.emg_timestamps;arrays['imu_timestamps']=fitted.imu_timestamps
    arrays['timestamp_basis']=np.array('sequence-fit to host arrival; sparse splice recovered; originals retained; not hardware synchronization')
    info.update(original_sha256=source_sha,backup=str(backup),removed_frame_hex=[raw[i].tobytes().hex() for i in info['removed_raw_indices']])
    try:
        with tmp.open('wb') as f:np.savez(f,**arrays);f.flush();os.fsync(f.fileno())
        now=source.stat()
        if (now.st_ino,now.st_size,now.st_mtime_ns)!=signature:raise ValueError('source_changed_during_recovery')
        os.replace(tmp,source)
        _atomic_json(meta,dict(ok=True,note='fitted_after_verified_sparse_splice_recovery',
            emg_rate_hz=fitted.emg_rate_hz,n_anchors=fitted.n_anchors,residual_ms=fitted.residual_ms,shift_ms=fitted.shift_ms))
        info.update(applied=True,recovered_sha256=_digest(source))
        _atomic_json(directory/'postprocess_recovery.json',info)
        if os.name=='posix':
            fd=os.open(str(directory),os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
    except BaseException:
        shutil.copy2(backup/source.name,source)
        if (backup/meta.name).exists():shutil.copy2(backup/meta.name,meta)
        raise
    finally:
        if tmp.exists():tmp.unlink()
    return info
