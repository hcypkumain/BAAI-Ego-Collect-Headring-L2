#!/usr/bin/env python3
"""Framework adapters for Pi data, using the original checker APIs and thresholds."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import argparse
import csv
import dataclasses
from datetime import datetime
import json
import os
import hashlib
import stat
import re
from pathlib import Path
import sys
import numpy as np
import yaml

from project_paths import BASE, PROJECT, SRC, devices
sys.path.insert(0,str(PROJECT/'src'))
import embodied_brain_collect.checkers as framework
from embodied_brain_collect.checkers.base import BaseChecker, Finding, flatten_checks
from embodied_brain_collect.checkers.camera import CameraChecker
from embodied_brain_collect.checkers.checks import ts_checks,FrameCountMatch,BlackFrame,Freeze,NanFraction
from embodied_brain_collect.recorders.emg.timestamp_rebuild import rebuild
from embodied_brain_collect.session.config import load_checker
from embodied_brain_collect.stim.marker_codes import RUN_START,RUN_END
from embodied_brain_collect.visualizers.qc_page import build_page,Options
from eeg_qc import PiEegChecker,standardize_eeg
from mpp_qc import video_backend
from practical_evidence import PiTimingEvidence, PiVideoEvidence
from capture_integrity import checks_for_stream
from qc_policy import apply_policy, load_policy

def atomic_json(path, value):
    path=Path(path)
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,default=str)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory_fd=os.open(str(path.parent),os.O_RDONLY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)


def atomic_npz(path,**arrays):
    path=Path(path);tmp=path.with_suffix('.tmp')
    with tmp.open('wb') as f:
        np.savez(f,**arrays);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def _headband_rate(default=30.):
    """Configured headband camera rate.

    The retired ROS bridge effectively delivered 20 fps; camd delivers the rate
    in its own camd.conf (30 fps on EgoRecord v0.1.10). Reading it from the
    device configuration keeps QC from judging the stream against a stale rate.
    """
    try:
        cameras = (devices() or {}).get('cameras') or {}
    except Exception:
        return default
    rates = {int(item['fps']) for item in cameras.values()
             if isinstance(item, dict) and str(item.get('fps', '')).isdigit()}
    return float(rates.pop()) if len(rates) == 1 else default


HEADBAND_RATE = _headband_rate()


class PiCameraChecker(CameraChecker):
    matches=('cam_ros_',)
    checks=[ts_checks('frames',expected_rate=HEADBAND_RATE),FrameCountMatch(video='frames.mp4'),
            BlackFrame(video='frames.mp4'),Freeze(video='frames.mp4')]
    def prepare(self,ctx):
        ctx.add_series('frames',key='frames_timestamps',expected_rate=HEADBAND_RATE)

class BoardImuChecker(BaseChecker):
    name='board_imu'
    matches=('board_imu',)
    default_series='imu'
    checks=[ts_checks('imu',expected_rate=200.),NanFraction('angular_velocity'),
            NanFraction('linear_acceleration')]
    def prepare(self,ctx):
        ctx.add_series('imu',key='timestamps',expected_rate=200.)


PI_QC_STREAMS = frozenset(('cam_ros_left','cam_ros_right','cam_ros_bright','cam_ros_bleft',
                         'board_imu','cam_left_wrist','cam_right_wrist','emg_left','emg_right','eeg'))
PI_ROS_STREAMS = frozenset(('cam_ros_left','cam_ros_right','cam_ros_bright','cam_ros_bleft','board_imu'))
HEADBAND_QC_FILES = ('camera-camchain.yaml','camera_imu-camchain-imucam.yaml','camera_imu-imu.yaml')


def _qc_real_directory(path):
    if not stat.S_ISDIR(Path(path).lstat().st_mode):
        raise ValueError('not a real directory: '+Path(path).name)


def _qc_regular_bytes(path, limit=1024*1024, allow_empty=False):
    """Read bounded regular evidence without following a symlink or changed inode."""
    path=Path(path);before=path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_size>limit
            or (not allow_empty and before.st_size==0)):
        raise ValueError('not a bounded regular file: '+path.name)
    # Windows stat/fstat expose different ctime semantics on some runtimes.
    signature=lambda value:((value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
                            +((value.st_ctime_ns,) if os.name!='nt' else ()))
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        opened=os.fstat(fd)
        if signature(opened)!=signature(before) or not stat.S_ISREG(opened.st_mode):
            raise ValueError('evidence changed: '+path.name)
        with os.fdopen(fd,'rb',closefd=False) as handle:raw=handle.read(limit+1)
        if (len(raw)!=before.st_size or signature(os.fstat(fd))!=signature(before)
                or signature(path.lstat())!=signature(before)):
            raise ValueError('evidence changed: '+path.name)
        return raw
    finally:os.close(fd)


def _qc_json(path):
    def reject(value):raise ValueError('non-finite JSON: '+value)
    value=json.loads(_qc_regular_bytes(path).decode('utf-8'),parse_constant=reject)
    if not isinstance(value,dict):raise ValueError('JSON evidence is not an object: '+Path(path).name)
    return value


def _qc_headband_metadata(root):
    """Accept only the known calibration sidecar tree, never a raw recorder tree."""
    parent=Path(root)/'ego_headband';_qc_real_directory(parent)
    entries={p.name:p for p in parent.iterdir()}
    allowed={'.calibration.lock','calibration_status.json','calibrations','calibration_candidates'}
    if not set(entries)<=allowed or 'calibration_status.json' not in entries:
        raise ValueError('headband directory contains non-calibration or missing status files')
    if '.calibration.lock' in entries:_qc_regular_bytes(entries['.calibration.lock'],4096,True)
    status=_qc_json(entries['calibration_status.json'])
    if (status.get('version')!=1 or status.get('state') not in ('verified','candidate','not_available','disabled')
            or any(type(status.get(k)) is not bool for k in ('available','applicability_verified','conflict'))
            or not isinstance(status.get('reasons'),list) or not all(isinstance(v,str) for v in status['reasons'])
            or not isinstance(status.get('warnings'),list) or not all(isinstance(v,str) for v in status['warnings'])
            or not isinstance(status.get('cameras'),dict)):
        raise ValueError('invalid headband calibration status')
    snapshots=set(entries)&{'calibrations','calibration_candidates'}
    if len(snapshots)>1:raise ValueError('conflicting headband snapshot modes')
    if status['available']:
        verified=status['applicability_verified']
        expected='calibrations' if verified else 'calibration_candidates'
        if (snapshots!={expected} or status['state']!=('verified' if verified else 'candidate')
                or status.get('snapshot_relative_dir')!='ego_headband/'+expected):
            raise ValueError('headband snapshot/status mismatch')
        target=entries[expected];_qc_real_directory(target)
        if {p.name for p in target.iterdir()}!=set(HEADBAND_QC_FILES)|{'manifest.json'}:
            raise ValueError('headband snapshot contains non-calibration files')
        records=status.get('files')
        if (not isinstance(records,list) or len(records)!=3
                or [r.get('name') if isinstance(r,dict) else None for r in records]!=list(HEADBAND_QC_FILES)):
            raise ValueError('invalid headband calibration file list')
        for record in records:
            if (set(record)!={'name','size','sha256'} or type(record['size']) is not int
                    or not 0<record['size']<=1024*1024 or not isinstance(record['sha256'],str)):
                raise ValueError('invalid headband calibration file record')
            raw=_qc_regular_bytes(target/record['name'])
            if len(raw)!=record['size'] or hashlib.sha256(raw).hexdigest()!=record['sha256']:
                raise ValueError('headband calibration bytes differ: '+record['name'])
        raw=_qc_regular_bytes(target/'manifest.json')
        if (json.loads(raw.decode('utf-8'))!={'version':1,'files':records}
                or hashlib.sha256(raw).hexdigest()!=status.get('manifest_sha256')):
            raise ValueError('headband calibration manifest differs')
    elif snapshots or status['applicability_verified']:
        raise ValueError('unavailable headband status conflicts with saved snapshot')
    return status


def _qc_reference_bundle_metadata(root):
    """Verify the frozen task sidecar; it is not a sensor recorder stream."""
    root=Path(root);parent=root/'reference_bundle';_qc_real_directory(parent)
    meta=_qc_json(root/'meta.json');bundle=meta.get('reference_bundle')
    if (meta.get('reference_layout_status')!='bundle_snapshot' or not isinstance(bundle,dict)
            or set(bundle)-{'task','notes','files'} or not isinstance(bundle.get('files'),dict)
            or not 1<=len(bundle['files'])<=4):
        raise ValueError('missing or invalid frozen task bundle proof')
    records=bundle['files']
    if {entry.name for entry in parent.iterdir()}!=set(records):
        raise ValueError('task bundle files differ from the frozen manifest')
    for name,record in records.items():
        if (not isinstance(name,str) or not name or name.startswith('.') or '/' in name or '\\' in name
                or len(name.encode('utf-8'))>384 or not isinstance(record,dict)
                or set(record)!={'path','sha256','size_bytes'} or record['path']!='reference_bundle/'+name
                or type(record['size_bytes']) is not int or not 0<record['size_bytes']<=2*1024*1024
                or not isinstance(record['sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',record['sha256']) is None):
            raise ValueError('invalid task bundle file record')
        raw=_qc_regular_bytes(parent/name,2*1024*1024)
        if len(raw)!=record['size_bytes'] or hashlib.sha256(raw).hexdigest()!=record['sha256']:
            raise ValueError('task bundle bytes differ: '+name)
    return dict(task=bundle.get('task'),files=sorted(records),file_count=len(records),verified=True)


def pi_qc_inputs(path):
    """Plan Pi dispatch using frozen selection; preserve all ordinary raw checks."""
    root=Path(path);findings=[];metadata=[];selected=None
    def finding(level,message,check,subject='',detail=None):
        findings.append(dict(level=level,message=message,check=check,subject=subject,detail=detail or {}))
    try:_qc_real_directory(root)
    except (OSError,ValueError) as exc:
        finding('ERROR','会话目录不是可验证的真实目录：'+str(exc),'PiSelection')
        return dict(directories=[],findings=findings,metadata_only=metadata)
    directories=[]
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            if child.is_dir() or child.name in PI_QC_STREAMS or child.name=='ego_headband':
                finding('ERROR','采集目录不能是符号链接：'+child.name,'PiSelection',child.name)
        elif child.is_dir():directories.append(child)
    # This directory contains YAML/CSV/overview PNG, not NPZ/video samples.
    # Validate its exact frozen manifest instead of applying StreamPresent.
    if (root/'reference_bundle').exists() or (root/'reference_bundle').is_symlink():
        directories=[p for p in directories if p.name!='reference_bundle']
        try:detail=_qc_reference_bundle_metadata(root)
        except (OSError,ValueError,TypeError,KeyError,RecursionError) as exc:
            finding('ERROR','任务包场景资料校验失败：'+str(exc),'ReferenceBundle','reference_bundle')
        else:
            metadata.append('reference_bundle')
            finding('INFO','任务包场景资料已保存，文件校验一致','ReferenceBundle','reference_bundle',detail)
    elif (root/'meta.json').is_file():
        try:bundle_expected=bool(_qc_json(root/'meta.json').get('reference_bundle'))
        except (OSError,ValueError,TypeError,RecursionError):bundle_expected=False
        if bundle_expected:finding('ERROR','本段应保存任务包场景资料，但目录缺失','ReferenceBundle','reference_bundle')
    selection=root/'device_selection.json'
    if selection.exists() or selection.is_symlink():
        try:
            frozen=_qc_json(selection);items=frozen.get('selected');config=frozen.get('device_config')
            if (not isinstance(items,list) or not items or len(items)>len(PI_QC_STREAMS)
                    or not all(isinstance(item,str) and item in PI_QC_STREAMS for item in items)
                    or len(set(items))!=len(items) or not isinstance(config,dict)):
                raise ValueError('invalid frozen Pi device selection')
            # EgoRecord v0.1.10 retired the ROS bridge: headband cameras are
            # identified by their camd name and the board IMU by its character
            # device. Keep demanding a traceable source, just the current one,
            # while still accepting a frozen pre-migration session.
            for item in items:
                if item.startswith('cam_ros_'):
                    camera=config.get('cameras',{}).get(item[len('cam_ros_'):],{})
                    if not isinstance(camera,dict):
                        raise ValueError('frozen camera configuration missing: '+item)
                    camd_name,topic=camera.get('camd'),camera.get('topic')
                    valid_camd=isinstance(camd_name,str) and bool(camd_name)
                    valid_topic=isinstance(topic,str) and topic.startswith('/')
                    if not (valid_camd or valid_topic):
                        raise ValueError('frozen camera source missing (camd or topic): '+item)
                    if valid_camd and not isinstance(camera.get('fps'),int):
                        raise ValueError('frozen camd camera rate missing: '+item)
                elif item=='board_imu':
                    board=config.get('board_imu',{})
                    if not isinstance(board,dict):
                        raise ValueError('frozen board IMU configuration missing')
                    device,topic=board.get('device'),board.get('topic')
                    valid_device=isinstance(device,str) and device.startswith('/dev/')
                    valid_topic=isinstance(topic,str) and topic.startswith('/')
                    if not (valid_device or valid_topic):
                        raise ValueError('frozen board IMU source missing (device or topic)')
            selected=set(items)
        except (OSError,ValueError,TypeError,AttributeError,RecursionError) as exc:
            finding('ERROR','冻结设备清单不可验证：'+str(exc),'PiSelection','device_selection.json')
    if selected is not None:
        actual={p.name for p in directories}
        for name in sorted(selected-actual):
            finding('ERROR',name+'/ 已选择但缺少真实数据目录','StreamPresent',name)
        if selected&PI_ROS_STREAMS and 'ego_headband' in actual:
            try:status=_qc_headband_metadata(root)
            except (OSError,ValueError,TypeError,RecursionError) as exc:
                finding('ERROR','头环标定侧车不可验证，保留原始流检查：'+str(exc),
                        'HeadbandCalibration','ego_headband')
            else:
                directories=[p for p in directories if p.name!='ego_headband'];metadata.append('ego_headband')
                detail={k:status.get(k) for k in ('state','available','applicability_verified','conflict',
                    'source_verified','reasons','warnings','cameras','snapshot_relative_dir','manifest_sha256')}
                level='ERROR' if status['conflict'] else 'INFO' if status['applicability_verified'] else 'WARN'
                message=('头环标定快照冲突' if status['conflict'] else '头环标定快照适用性已确认' if status['applicability_verified']
                         else '头环标定仅作候选元数据，适用性尚未确认；原原因与图像尺寸差异保留')
                finding(level,message,'HeadbandCalibration','ego_headband',detail)
    return dict(directories=directories,findings=findings,metadata_only=metadata)

def _frozen_reference_selection(old_meta):
    """Validate only the frozen per-segment catalog proof, never live settings."""
    if 'reference_selection' not in old_meta:
        return None
    value=old_meta['reference_selection']
    required={'version','catalog_revision','list_id','list_name','queue_sha256','position','total','asset'}
    def text(item,limit=384):
        return (isinstance(item,str) and 0<len(item.encode('utf-8'))<=limit
                and all(character.isprintable() for character in item))
    def token(item):
        return isinstance(item,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',item) is not None
    def digest(item):
        return isinstance(item,str) and re.fullmatch(r'[0-9a-f]{64}',item) is not None
    if (not isinstance(value,dict) or set(value)!=required or type(value['version']) is not int
            or value['version']!=1 or type(value['catalog_revision']) is not int
            or not 0<=value['catalog_revision']<=9007199254740991
            or not token(value['list_id']) or not text(value['list_name'])
            or not digest(value['queue_sha256']) or type(value['position']) is not int
            or type(value['total']) is not int or not 1<=value['position']<=value['total']<=5000):
        raise ValueError('invalid frozen reference-list selection')
    asset=value['asset']
    fields={'id','name','origin','relative_path','sha256'}
    if (not isinstance(asset,dict) or not fields<=set(asset)
            or not set(asset)<=fields|{'original_filename'} or not token(asset['id'])
            or not text(asset['name']) or asset['origin'] not in ('upload','legacy')
            or not digest(asset['sha256'])
            or ('original_filename' in asset and not text(asset['original_filename']))):
        raise ValueError('invalid frozen reference asset')
    relative=asset['relative_path']
    if (not text(relative,1024) or '\\' in relative or ':' in relative
            or not relative.startswith('configs/environments/')
            or any(part in ('','.','..') for part in relative.split('/'))
            or relative!=old_meta.get('reference_image')
            or asset['sha256']!=old_meta.get('reference_sha256')):
        raise ValueError('reference selection differs from the frozen image')
    upload_root='configs/environments/_uploads/'
    if asset['origin']=='upload':
        if relative!=upload_root+asset['id']+'_1.png':
            raise ValueError('uploaded reference path does not match its asset identity')
    elif relative.startswith(upload_root):
        raise ValueError('uploaded reference cannot be marked as a legacy environment')
    return dict(value,asset=dict(asset))


def _frozen_session_identity(path,old_meta,require=False):
    """Legacy data keep their old metadata; new captures must match the leaf."""
    keys=('collector_id','session','session_id','session_timezone',
          'session_created_unix','session_name_format')
    advertised=any(key in old_meta for key in keys[2:])
    if not advertised and not require:
        return None
    if any(key not in old_meta for key in keys):
        raise ValueError('incomplete frozen collector/session identity')
    from session_naming import session_identity
    identity=session_identity(old_meta['collector_id'],old_meta['session_created_unix'])
    if (any(old_meta[key]!=identity[key] for key in keys)
            or Path(path).name!=identity['session_id']):
        raise ValueError('collector/session metadata does not match the source folder')
    return identity


def _write_pi_session_metadata(path,summary,timestamps,old_meta,names):
    """Keep original metadata writer, with frozen identity and uploaded tasks."""
    selection=_frozen_reference_selection(old_meta)
    identity=_frozen_session_identity(path,old_meta,require=selection is not None)
    from embodied_brain_collect.session.launcher import _write_session_meta
    from embodied_brain_collect.session.environment import scene_layout
    reference=old_meta.get('reference_image')
    prefix='configs/environments/'
    environment=reference[len(prefix):] if reference and reference.startswith(prefix) else reference
    uploaded=selection is not None and selection['asset']['origin']=='upload'
    # A raw uploaded picture has no evidenced scene/placements. Leaving its
    # environment unset also prevents the original packer trying to rebuild a
    # nonexistent layout later. Preserve reference_image and selection instead.
    layout=None if uploaded else scene_layout(environment) if environment else None
    arguments=dict(environment=None if uploaded else environment,recorders=names,layout=layout)
    if identity is not None:
        arguments['collect_info']=dict(identity)
    _write_session_meta(path,**arguments)
    meta=yaml.safe_load((path/'meta.yaml').read_text(encoding='utf-8')) or {}
    meta.update(status='success',started_at=datetime.fromtimestamp(float(timestamps[0])).isoformat(timespec='seconds'),start_time=float(timestamps[0]),end_time=float(timestamps[1]),pack_episode=False,max_runs=1,
        duration=float(timestamps[1]-timestamps[0]),reference_image=old_meta.get('reference_image'),
        reference_sha256=old_meta.get('reference_sha256'),stim=None,
        recorders=names,headband_calibration=summary.get('headband_calibration'),
        pi=old_meta)
    if identity is not None:
        meta.update(identity)
    if selection is not None:
        meta.update(reference_selection=selection,reference_list_id=selection['list_id'],
                    reference_list_name=selection['list_name'],reference_queue_sha256=selection['queue_sha256'],
                    reference_position=selection['position'],reference_total=selection['total'])
    if uploaded:
        meta.update(environment=None,scene=None,objects=None,notes=None,
                    task_name=selection['asset']['name'],reference_layout_status='bundle_snapshot' if old_meta.get('reference_bundle') else 'not_provided')
        if old_meta.get('reference_bundle'):meta['reference_bundle']=old_meta['reference_bundle']
    (path/'meta.yaml').write_text(yaml.safe_dump(meta,allow_unicode=True,sort_keys=False),encoding='utf-8')


def standardize(path):
    path=Path(path)
    summary=json.loads((path/'capture_summary.json').read_text())
    if summary.get('status') not in ('success','qc_error'):
        raise RuntimeError('Only completed native captures can be standardized')
    for name in ('emg_left','emg_right'):
        if name not in summary:continue
        directory=path/name
        output=directory/(name+'.npz')
        if output.exists():
            continue
        chunks=sorted(directory.glob('chunk_*.npz'))
        if not chunks:raise RuntimeError(name+': missing source chunks')
        parts={}
        for chunk in chunks:
            with np.load(chunk,allow_pickle=False) as z:
                for key in z.files:
                    if key=='timestamp_basis':continue
                    parts.setdefault(key,[]).append(z[key])
        arrays={key:np.concatenate(value,axis=0) for key,value in parts.items()}
        arrays['emg_sn']=arrays['emg_sn'].astype(np.int64)
        arrays['imu_sn']=arrays['imu_sn'].astype(np.int64)
        result=rebuild(arrays['emg_arrival_timestamps'],arrays['emg_sn'],
                       arrays['imu_arrival_timestamps'],arrays['imu_sn'])
        atomic_json(directory/'timestamp_rebuild.json',dict(
            ok=result.ok,note=result.note,emg_rate_hz=result.emg_rate_hz,
            n_anchors=result.n_anchors,residual_ms=result.residual_ms,shift_ms=result.shift_ms))
        if result.ok:
            arrays['emg_timestamps']=result.emg_timestamps
            arrays['imu_timestamps']=result.imu_timestamps
        arrays['timestamp_basis']=np.array('sequence-fit to host arrival; raw arrival preserved; not hardware synchronization' if result.ok else 'host arrival; reconstruction refused')
        atomic_npz(output,**arrays)
        # Archive raw chunks below the stream: core checker chooses the sole top-level NPZ.
        archive=directory/'raw_chunks';archive.mkdir(exist_ok=True)
        for chunk in chunks:chunk.rename(archive/chunk.name)
        if len(arrays['emg_data'])!=summary[name]['emg_frames']:
            raise RuntimeError(name+': merged sample count mismatch')
    for directory in sorted(path.glob('cam_ros_*')):
        with (directory/'timestamps.csv').open(encoding='utf-8') as f:rows=list(csv.DictReader(f))
        if not rows:continue
        # camd sessions record camd_ts_ns and a keyframe flag; sessions captured
        # before the EgoRecord v0.1.10 migration carry ros_stamp_ns/ros_sequence.
        stamp_field='camd_ts_ns' if 'camd_ts_ns' in rows[0] else 'ros_stamp_ns'
        sequence_field='keyframe' if 'keyframe' in rows[0] else 'ros_sequence'
        stamps=np.array([int(r[stamp_field]) for r in rows],dtype=np.int64)
        arrays=dict(frames_timestamps=stamps/1e9,
            arrival_unix_ns=np.array([int(r['arrival_unix_ns']) for r in rows],dtype=np.int64),
            arrival_monotonic_ns=np.array([int(r['arrival_monotonic_ns']) for r in rows],dtype=np.int64),
            video_pts_ns=np.array([int(r['video_pts_ns']) for r in rows],dtype=np.int64))
        arrays[stamp_field]=stamps
        arrays[sequence_field]=np.array([int(r[sequence_field]) for r in rows],dtype=np.int64)
        atomic_npz(directory/(directory.name+'.npz'),**arrays)
        standard=directory/'frames.mp4'
        if not standard.exists():os.link(directory/'video.mp4',standard)  # Read legacy sessions only.
    for name in ('cam_left_wrist','cam_right_wrist'):
        if name not in summary:continue
        directory=path/name
        with (directory/'timestamps.csv').open() as f:rows=list(csv.DictReader(f))
        arrays={key:np.array([int(r[key]) for r in rows],dtype=np.int64) for key in
                ('frames_unix_ns','arrival_unix_ns','arrival_monotonic_ns','video_pts_ns','source_pts_ns')}
        if len(rows)!=summary[name]['frames']:raise RuntimeError(name+': timestamp/frame mismatch')
        atomic_npz(directory/(name+'.npz'),frames_timestamps=arrays.pop('frames_unix_ns')/1e9,
            timestamp_basis=np.array('host frame arrival; not hardware synchronization'),**arrays)
    directory=path/'board_imu'
    if directory.exists():
        with (directory/'raw.csv').open() as f:rows=list(csv.DictReader(f))
        def values(prefix,names):
            return np.array([[float(row[prefix+n]) for n in names] for row in rows],dtype=np.float64)
        stamp=np.array([int(row['ros_stamp_ns']) for row in rows],dtype=np.int64)
        atomic_npz(directory/'board_imu.npz',timestamps=stamp/1e9,ros_stamp_ns=stamp,
            arrival_unix_ns=np.array([int(row['arrival_unix_ns']) for row in rows],dtype=np.int64),
            arrival_monotonic_ns=np.array([int(row['arrival_monotonic_ns']) for row in rows],dtype=np.int64),
            sequence=np.array([int(row['sequence']) for row in rows],dtype=np.int64),
            orientation_valid=np.linalg.norm(values('orientation_','xyzw'),axis=1)>1e-6,
            orientation=values('orientation_','xyzw'),angular_velocity=values('angular_velocity_','xyz'),
            linear_acceleration=values('linear_acceleration_','xyz'),
            orientation_covariance=values('orientation_covariance_',list(map(str,range(9)))),
            angular_velocity_covariance=values('angular_velocity_covariance_',list(map(str,range(9)))),
            linear_acceleration_covariance=values('linear_acceleration_covariance_',list(map(str,range(9)))))
    standardize_eeg(path,summary,atomic_npz)
    marker=path/'marker';marker.mkdir(exist_ok=True)
    timestamps=np.array([summary['start_unix_ns']/1e9,summary['stop_unix_ns']/1e9])
    atomic_npz(marker/'marker.npz',marker_code=np.array([RUN_START,RUN_END],dtype=np.int64),
        marker_tag=np.array(['RUN_START','RUN_END']),
        marker_t_sent_pc=timestamps,marker_t_local_recv=timestamps,
        source=np.array('Pi controller software start/stop gate; no TTL sent'))
    old_meta=json.loads((path/'meta.json').read_text(encoding='utf-8')) if (path/'meta.json').exists() else {}
    names={name:('OmniBCI ADS1299' if name=='eeg' else 'WLEMGUV2A' if name.startswith('emg_') else 'Pi ROS IMU' if name=='board_imu' else 'Pi USB wrist camera' if name in ('cam_left_wrist','cam_right_wrist') else 'Pi ROS fisheye')
           for name in summary if name.startswith(('cam_ros_','emg_','board_imu')) or name in ('cam_left_wrist','cam_right_wrist','eeg')}
    _write_pi_session_metadata(path,summary,timestamps,old_meta,names)
    return summary

def performance():
    return yaml.safe_load((BASE/'configs/performance.yaml').read_text())

def parallel_qc_session(path, cfg, workers=None, progress=None):
    """Same original checkers, thresholds and report order; concurrent independent streams."""
    from concurrent.futures import ThreadPoolExecutor
    import threading,time
    from embodied_brain_collect.checkers.base import SessionReport,StreamReport,Finding
    from embodied_brain_collect.checkers.marker import find_run_window
    root=Path(path);window=find_run_window(root)
    inputs=pi_qc_inputs(root)
    if window is None:
        report=framework.qc_session(root,checker_cfg=cfg)
        report.findings.extend(Finding(**item) for item in inputs['findings'])
        # A missing marker remains an original hard failure, but must not
        # accidentally bypass the modality-specific continuity checks.
        from embodied_brain_collect.checkers.base import CheckContext
        for name, stream in report.streams.items():
            context=CheckContext(stream=name,directory=root/name,window=None)
            try:
                for extra in checks_for_stream(name):
                    result=extra.run(context)
                    stream.findings.extend(result.findings)
                    stream.stats[extra.name]=result.stats
            finally:context.close()
        return report
    config=performance()
    mem={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    available=mem['MemAvailable']//1024
    count=workers if workers is not None else min(int(config['qc_workers']),max(1,(available-int(config['qc_reserved_available_mb']))//int(config['qc_min_available_mb_per_worker'])))
    count=max(1,min(3,count))
    report=SessionReport(session_dir=str(root),window=window)
    report.findings.extend(Finding(**item) for item in inputs['findings'])
    classes=(PiEegChecker,BoardImuChecker,PiCameraChecker,CameraChecker)+tuple(c for c in framework.CHECKERS if c not in (CameraChecker,PiCameraChecker,BoardImuChecker,PiEegChecker))
    def check(directory):
        cls=next((c for c in classes if directory.name.startswith(c.matches)),None)
        if cls is None:
            return StreamReport(stream=directory.name,files=sorted(p.name for p in directory.iterdir() if p.is_file()),
                findings=[Finding('INFO','未知模态 — 已跳过',check='Dispatch')])
        checker=cls(cfg)
        # Instance-local additions reuse each original context and decoder.
        # Class-level check lists, checker thresholds and parallelism stay intact.
        additions=[PiTimingEvidence()]+list(checks_for_stream(directory.name))
        if isinstance(checker,CameraChecker):
            black_cfg=next((v for k,v in cfg.items() if k.lower().replace('_','')=='blackframe'),{})
            lum=black_cfg.get('lum_thr',8.) if isinstance(black_cfg,dict) else 8.
            additions.append(PiVideoEvidence(lum_thr=float(lum)))
        checker.checks=tuple(flatten_checks(checker.checks))+tuple(additions)
        return checker.run(directory,window)
    futures={}
    started=time.monotonic();timings={};active=set();lock=threading.Lock()
    directories=inputs['directories']
    total=sum(framework._has_data(d) for d in directories)
    def measured(directory):
        begin=time.monotonic()
        with lock:
            active.add(directory.name)
            if progress:progress(dict(completed=len(timings),total=total,active=sorted(active),elapsed_seconds=time.monotonic()-started))
        try:return check(directory)
        finally:
            with lock:
                timings[directory.name]=time.monotonic()-begin;active.discard(directory.name)
                if progress:progress(dict(completed=len(timings),total=total,active=sorted(active),elapsed_seconds=time.monotonic()-started))
    with ThreadPoolExecutor(max_workers=count,thread_name_prefix='qc') as pool:
        for directory in directories:
            if not framework._has_data(directory):
                report.empty_dirs.append(directory.name)
                report.findings.append(Finding('ERROR',directory.name+'/ 无数据文件 — recorder 未保存',check='StreamPresent',subject=directory.name))
            else:futures[directory.name]=pool.submit(measured,directory)
        for name,future in futures.items():report.streams[name]=future.result()
    report.pi_timing={'workers':count,'streams':timings,'elapsed_seconds':time.monotonic()-started,
                      'metadata_only_directories':inputs['metadata_only']}
    return report

def check_session(path,html=False,progress=None):
    import cv2
    cv2.setNumThreads(1)
    original=framework.CHECKERS
    framework.CHECKERS=(PiEegChecker,BoardImuChecker,PiCameraChecker,CameraChecker)+tuple(c for c in original if c not in (CameraChecker,PiCameraChecker,BoardImuChecker,PiEegChecker))
    try:
        cfg=load_checker()
        settings=performance()
        black_cfg=next((v for k,v in cfg.items() if k.lower().replace('_','')=='blackframe'),{})
        settings['qc_mpp_black_lum_thr']=black_cfg.get('lum_thr',8.) if isinstance(black_cfg,dict) else 8.
        with video_backend(settings) as video_decodes:
            report=parallel_qc_session(path,cfg,progress=progress)
        if hasattr(report,'pi_timing'):
            report.pi_timing['video_decodes']=video_decodes
    finally:
        framework.CHECKERS=original
    # The original findings remain available. Only the explicit Pi acceptance
    # policy determines which results prevent saving and normal downstream pack.
    raw=report.to_dict()
    raw_path=Path(path)/'qc_raw_report.json'
    atomic_json(raw_path,raw)
    try:
        policy_config=load_policy(BASE/'configs/qc_policy.json')
        report,audit=apply_policy(report,policy_config)
    except ValueError as exc:
        import copy
        report=copy.deepcopy(report)
        report.findings.append(Finding('ERROR','QC 策略配置无效：'+str(exc),check='PiPolicyConfig'))
        audit=dict(schema_version=1,preset='invalid_configuration',mode='invalid',
                   raw_level=raw['level'],effective_level=report.level,error=str(exc))
    audit.update(raw_report='qc_raw_report.json',
                 raw_report_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
    atomic_json(Path(path)/'qc_policy.json',audit)
    report.qc_raw_level=raw['level']
    report.qc_policy=audit.get('preset','practical-v1')
    data=report.to_dict()
    data['pi_qc_policy']=dict(preset=report.qc_policy,raw_level=raw['level'],
                             effective_level=report.level,audit='qc_policy.json',
                             raw_report='qc_raw_report.json',
                             raw_report_sha256=audit['raw_report_sha256'])
    atomic_json(Path(path)/'qc_report.json',data)
    if hasattr(report,'pi_timing'):atomic_json(Path(path)/'qc_timing.json',report.pi_timing)
    if html or cfg.get('html',False):
        (Path(path)/'qc.html').write_text(build_page(data,session_dir=path,opt=Options(frames=False,filter=False)),encoding='utf-8')
    framework.print_report(report)
    return report

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('session',type=Path);ap.add_argument('--standardize',action='store_true');ap.add_argument('--html',action='store_true')
    args=ap.parse_args()
    if args.standardize:standardize(args.session)
    report=check_session(args.session,args.html)
    sys.exit({'INFO':0,'WARN':1,'ERROR':2}[report.level])
