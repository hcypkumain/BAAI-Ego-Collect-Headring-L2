"""Read-only saved-recording catalog and bounded signal windows for the phone interface.

No imports of acquisition owners and no device opens. Video decoding is lazy and
delegated to replay_video; original recordings and QC reports are never written.
"""
import csv
import hashlib
import hmac
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import struct
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zipfile
from qc_details import summarize_qc,compact_qc

import numpy as np

READ_PEERS = frozenset(('127.0.0.1', '::1'))
CAMERAS = ('cam_ros_left', 'cam_ros_right', 'cam_ros_bleft', 'cam_ros_bright',
           'cam_left_wrist', 'cam_right_wrist')
SIGNALS = ('eeg', 'emg_left', 'emg_right', 'emg_left_imu', 'emg_right_imu', 'board_imu')
LABELS = dict(cam_ros_left='头环前左', cam_ros_right='头环前右', cam_ros_bleft='头环后左',
              cam_ros_bright='头环后右', cam_left_wrist='左腕相机', cam_right_wrist='右腕相机',
              eeg='脑电', emg_left='左臂肌电', emg_right='右臂肌电',
              emg_left_imu='左臂惯性', emg_right_imu='右臂惯性', board_imu='头环惯性')
ID_RE = re.compile(r'^[0-9a-f]{24}$')
MAX_JSON = 2 * 1024 * 1024
MAX_SESSIONS = 2000
MAX_CHUNKS = 100000
MAX_READ = 8 * 1024 * 1024
MAX_WINDOW_SAMPLES = 200000
MAX_CSV_ROWS = 2000000
MAX_ZIP_FIELDS = 128
MAX_ZIP_DIRECTORY = 256 * 1024
ACTIVE_STATES = frozenset(('recording', 'collecting', 'preparing', 'ready', 'initializing',
                           'stopping', 'finalizing', 'checking', 'processing'))


class ReplayError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _number(value, default=0.0):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _int(value, default=0):
    if isinstance(value,bool):
        return default
    if isinstance(value,int):
        return value if 0<=value<=9223372036854775807 else default
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,19}',value):
        parsed=int(value)
        return parsed if parsed<=9223372036854775807 else default
    value = _number(value, default)
    return int(value) if 0 <= value <= 9223372036854775807 else default


def _bounded_csv_lines(handle,deadline):
    """Bound each physical line before csv can buffer a malformed huge field."""
    while True:
        if time.monotonic()>deadline:
            raise ReplayError('csv_read_timeout',503)
        line=handle.readline(16385)
        if not line:return
        if len(line)>16384:
            raise ReplayError('csv_line_too_large',413)
        yield line


def _check_npz_directory(path):
    """Bound ZIP central-directory allocation before zipfile parses it.

    Normal force-ZIP64 *members* need no special handling. A global ZIP64
    directory is accepted only in the fixed, single-disk layout Python supports;
    its counts and sizes are read from a 56-byte record, never an unbounded blob.
    """
    with Path(path).open('rb') as handle:
        handle.seek(0,os.SEEK_END)
        size=handle.tell()
        tail_size=min(size,65557)
        handle.seek(size-tail_size)
        tail=handle.read(tail_size)
        at=tail.rfind(b'PK\x05\x06')
        if at<0 or len(tail)-at<22:
            raise ReplayError('invalid_npz_directory',422)
        disk,cd_disk,disk_entries,entries,cd_size,cd_offset,comment_size=struct.unpack('<4H2IH',tail[at+4:at+22])
        eocd_offset=size-tail_size+at
        if at+22+comment_size!=len(tail) or disk or cd_disk:
            raise ReplayError('unsupported_npz_directory_layout',422)
        directory_boundary=eocd_offset
        if (disk_entries==65535 or entries==65535 or cd_size==0xffffffff or cd_offset==0xffffffff):
            if eocd_offset<20:
                raise ReplayError('invalid_zip64_locator',422)
            handle.seek(eocd_offset-20)
            locator=handle.read(20)
            if len(locator)!=20 or locator[:4]!=b'PK\x06\x07':
                raise ReplayError('invalid_zip64_locator',422)
            locator_disk,record_offset,total_disks=struct.unpack('<IQI',locator[4:])
            if locator_disk or total_disks!=1 or record_offset+56!=eocd_offset-20:
                raise ReplayError('unsupported_zip64_directory_layout',422)
            handle.seek(record_offset)
            record=handle.read(56)
            if len(record)!=56 or record[:4]!=b'PK\x06\x06':
                raise ReplayError('invalid_zip64_directory',422)
            record_size,_,_,disk,cd_disk,disk_entries,entries,cd_size,cd_offset=struct.unpack('<Q2H2I4Q',record[4:])
            if record_size!=44 or disk or cd_disk:
                raise ReplayError('unsupported_zip64_directory_layout',422)
            directory_boundary=record_offset
        if entries!=disk_entries or entries>MAX_ZIP_FIELDS or cd_size>MAX_ZIP_DIRECTORY:
            raise ReplayError('npz_directory_exceeds_limit',413)
        if cd_offset>directory_boundary or cd_size>directory_boundary-cd_offset:
            raise ReplayError('invalid_npz_directory_bounds',422)


class NpzReader:
    """Read NPY row ranges without loading a whole long recording into RAM.

    np.savez uses ZIP_STORED; those members are read directly by byte offset.
    Compressed legacy members use bounded streaming reads with a time budget.
    Objects/pickle, Fortran matrices and unexpected dimensions are refused.
    """
    def __init__(self, path, deadline=None):
        self.deadline = deadline if deadline is not None else time.monotonic()+5.0
        if time.monotonic()>self.deadline:
            raise ReplayError('recording_read_timeout',503)
        self.path = Path(path)
        _check_npz_directory(self.path)
        if time.monotonic()>self.deadline:
            raise ReplayError('recording_read_timeout',503)
        self.zip = zipfile.ZipFile(str(path), 'r')
        if time.monotonic()>self.deadline:
            self.zip.close()
            raise ReplayError('recording_read_timeout',503)
        if len(self.zip.infolist()) > MAX_ZIP_FIELDS:
            self.zip.close()
            raise ReplayError('too_many_npz_fields', 422)
        names=[i.filename for i in self.zip.infolist()]
        if len(names)!=len(set(names)):
            self.zip.close()
            raise ReplayError('duplicate_npz_fields',422)
        self.keys = {i.filename[:-4] for i in self.zip.infolist() if i.filename.endswith('.npy')}
        self.meta = {}

    def close(self):
        self.zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def describe(self, key):
        if key in self.meta:
            return self.meta[key]
        if key not in self.keys:
            raise ReplayError('missing_array:' + key, 422)
        info = self.zip.getinfo(key + '.npy')
        if info.flag_bits & 1:
            raise ReplayError('encrypted_npz_unsupported', 422)
        with self.zip.open(info) as handle:
            version = np.lib.format.read_magic(handle)
            length_size=2 if version==(1,0) else 4 if version==(2,0) else 0
            if not length_size:raise ReplayError('unsupported_npy_version',422)
            length_raw=handle.read(length_size)
            if len(length_raw)!=length_size:raise ReplayError('truncated_npy_header',422)
            length=int.from_bytes(length_raw,'little')
            if length>65536:raise ReplayError('npy_header_too_large',413)
            header=handle.read(length)
            if len(header)!=length:raise ReplayError('truncated_npy_header',422)
            # Bound the header before handing it to NumPy's literal parser.
            bounded_header=BytesIO(length_raw+header)
            if version == (1, 0):
                shape, fortran, dtype = np.lib.format.read_array_header_1_0(bounded_header)
            elif version == (2, 0):
                shape, fortran, dtype = np.lib.format.read_array_header_2_0(bounded_header)
            offset = handle.tell()
        if dtype.hasobject or dtype.kind not in 'biufSU' or len(shape) > 2:
            raise ReplayError('unsafe_or_unsupported_array:' + key, 422)
        if fortran and len(shape) > 1:
            raise ReplayError('fortran_array_unsupported:' + key, 422)
        if any(not isinstance(n, int) or n < 0 for n in shape):
            raise ReplayError('invalid_array_shape', 422)
        size = int(np.prod(shape, dtype=np.int64)) if shape else 1
        if size < 0 or size * dtype.itemsize != info.file_size - offset:
            raise ReplayError('truncated_or_invalid_array:' + key, 422)
        with self.path.open('rb') as raw:
            raw.seek(info.header_offset)
            header = raw.read(30)
        if len(header) != 30 or header[:4] != b'PK\x03\x04':
            raise ReplayError('invalid_zip_header', 422)
        absolute = info.header_offset + 30 + struct.unpack('<HH', header[26:30])[0] + struct.unpack('<HH', header[26:30])[1] + offset
        item = dict(shape=shape, dtype=dtype, offset=offset, absolute=absolute, info=info)
        self.meta[key] = item
        return item

    def rows(self, key, begin=0, end=None):
        if time.monotonic()>self.deadline:
            raise ReplayError('recording_read_timeout',503)
        meta = self.describe(key)
        shape, dtype = meta['shape'], meta['dtype']
        if not shape:
            begin, end, row_bytes, outshape = 0, 1, dtype.itemsize, ()
        else:
            end = shape[0] if end is None else end
            if not 0 <= begin <= end <= shape[0]:
                raise ReplayError('array_row_range_invalid', 422)
            row_bytes = dtype.itemsize * (shape[1] if len(shape) == 2 else 1)
            outshape = (end - begin,) + shape[1:]
        length = (end - begin) * row_bytes
        if length > MAX_READ:
            raise ReplayError('array_window_too_large', 413)
        if meta['info'].compress_type == zipfile.ZIP_STORED:
            with self.path.open('rb') as handle:
                handle.seek(meta['absolute'] + begin * row_bytes)
                raw = handle.read(length)
        else:
            # Do not use np.load or ZipExtFile.seek, which can hide an unbounded
            # decompression scan. Keep memory and wall time explicitly bounded.
            deadline = self.deadline
            skip = meta['offset'] + begin * row_bytes
            with self.zip.open(meta['info']) as handle:
                while skip:
                    if time.monotonic() > deadline:
                        raise ReplayError('compressed_array_seek_timeout', 503)
                    block = handle.read(min(skip, 1024 * 1024))
                    if not block:
                        raise ReplayError('truncated_array', 422)
                    skip -= len(block)
                raw = handle.read(length)
        if len(raw) != length:
            raise ReplayError('truncated_array', 422)
        return np.frombuffer(raw, dtype=dtype).reshape(outshape).copy()

    def scalar(self, key, default=None):
        if key not in self.keys:
            return default
        shape = self.describe(key)['shape']
        if shape not in ((), (1,)):
            return default
        value = self.rows(key)
        return value.item()


class RecordingReplay:
    def __init__(self, root, video_decoder=None, active_check=None, allow_missing_root=False):
        self.root = Path(root).absolute()
        self.allow_missing_root = allow_missing_root
        self.video_decoder = video_decoder
        self.active_check = active_check
        self.lock = threading.RLock()
        self._catalog = {}
        self._ordered = []
        self._refreshed = 0.0
        self._truncated = False
        self._json_cache={};self._json_cache_bytes=0

    def _safe(self, path, required=False):
        path = Path(path)
        try:
            path.relative_to(self.root)
            relative = path.relative_to(self.root)
            current = self.root
            if current.is_symlink():
                raise ReplayError('symlink_recording_root_forbidden', 403)
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise ReplayError('symlink_recording_path_forbidden', 403)
            path.resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            raise ReplayError('recording_path_forbidden', 403)
        if required and not path.is_file():
            raise ReplayError('recording_file_missing', 404)
        return path

    def _json(self, path, default=None):
        path = self._safe(path)
        if not path.is_file():
            return {} if default is None else default
        if path.stat().st_size > MAX_JSON:
            raise ReplayError('recording_metadata_too_large', 413)
        info=path.stat();key=(info.st_dev,info.st_ino,info.st_mtime_ns,info.st_size)
        with self.lock:
            cached=self._json_cache.get(str(path))
            if cached and cached[0]==key:return cached[1]
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError, OSError):
            raise ReplayError('recording_metadata_corrupt:' + path.name, 422)
        if not isinstance(value, dict):
            raise ReplayError('recording_metadata_not_object:' + path.name, 422)
        after=path.stat()
        if key==(after.st_dev,after.st_ino,after.st_mtime_ns,after.st_size) and info.st_size<=256*1024:
            with self.lock:
                previous=self._json_cache.pop(str(path),None)
                if previous:self._json_cache_bytes-=previous[2]
                while self._json_cache and (len(self._json_cache)>=32 or self._json_cache_bytes+info.st_size>2*1024*1024):
                    old=self._json_cache.pop(next(iter(self._json_cache)));self._json_cache_bytes-=old[2]
                self._json_cache[str(path)]=(key,value,info.st_size);self._json_cache_bytes+=info.st_size
        return value

    def _guard(self):
        if self.active_check is not None:
            try:
                busy = self.active_check()
            except ReplayError:
                self.release()
                raise
            except Exception:
                self.release()
                raise ReplayError('cannot_confirm_capture_idle', 503)
            if busy:
                self.release()
                raise ReplayError('采集或质量检验进行中，请完成后回看', 409)

    @staticmethod
    def _excluded(folder):
        # A cached catalog must never resurrect an explicitly discarded take.
        return (not folder.is_dir() or folder.is_symlink() or any((folder/name).exists() for name in (
            'discard_request.json','discard_cleanup.json','recording_deleted.json',
            'preparation_abandoned.json','ui_preparation_cancelled.json')))

    def _refresh(self, force=False):
        with self.lock:
            if self.allow_missing_root and not self.root.exists() and not self.root.is_symlink():
                # ReplayRuntime has already verified the configured SD mount
                # and UUID. A profile with no takes has no data directory yet.
                self._catalog={};self._ordered=[];self._truncated=False;self._refreshed=time.monotonic()
                return
            if not self.root.is_dir() or self.root.is_symlink():
                raise ReplayError('recording_storage_unavailable', 503)
            if not force and time.monotonic() - self._refreshed < 5.0:
                return
            candidates = []
            # Actual layout: data/<shift>/<session>. Also accept historical
            # data/<session> without walking raw chunks or media subtrees.
            for first in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
                if not first.is_dir() or first.is_symlink():
                    continue
                if (first/'capture_summary.json').is_file():
                    candidates.append(first)
                else:
                    for second in sorted(first.iterdir(), key=lambda p: p.name, reverse=True):
                        if second.is_dir() and not second.is_symlink() and (second/'capture_summary.json').is_file():
                            candidates.append(second)
                            if len(candidates)>MAX_SESSIONS:
                                break
                if len(candidates) > MAX_SESSIONS:
                    break
            candidates.sort(key=lambda p: p.name, reverse=True)
            self._truncated = len(candidates) > MAX_SESSIONS
            catalog = {}
            for folder in candidates[:MAX_SESSIONS]:
                if self._excluded(folder):continue
                try:
                    self._safe(folder)
                    summary = self._json(folder/'capture_summary.json')
                    state = self._json(folder/'status.json')
                    stopped = self._json(folder/'stop_gate.json')
                    final = summary.get('status') in ('success', 'qc_error', 'failed', 'error', 'cancelled', 'canceled', 'stopped')
                    # A directory with an open recording gate is never a replay.
                    if state.get('recording_active') is True or stopped.get('recording_active') is True:
                        continue
                    if state.get('state') in ACTIVE_STATES and not stopped:
                        continue
                    if not stopped and not final:
                        continue
                    start = self._json(folder/'start_gate.json')
                    start_ns = _int(start.get('start_unix_ns', summary.get('start_unix_ns')))
                    stop_ns = _int(stopped.get('stop_unix_ns', summary.get('stop_unix_ns')))
                    duration = max(0., (stop_ns-start_ns)/1e9) if start_ns and stop_ns else _number(summary.get('duration_seconds'))
                    identifier = hashlib.sha256(str(folder.relative_to(self.root)).encode('utf-8')).hexdigest()[:24]
                    status = str(summary.get('status') or 'stopped')[:48]
                    if (folder/'ui_preparation_cancelled.json').is_file():
                        status = 'cancelled'
                    qc = str(summary.get('qc_effective_level') or summary.get('qc_level') or state.get('qc_level') or 'UNKNOWN')[:24]
                    # Preparation failures have no START. Keep their real stop
                    # time in the chronology without inventing a recording gate.
                    attempt_ns = start_ns or stop_ns or _int(_number(state.get('time'))*1e9) or folder.stat().st_mtime_ns
                    failed = status in ('error','failed','qc_error') or bool(summary.get('errors'))
                    if qc == 'UNKNOWN' and failed:qc = 'ERROR'
                    count = sum(1 for n in CAMERAS+('eeg','emg_left','emg_right','board_imu') if n in summary)
                    catalog[identifier] = dict(path=folder, summary=summary, start=start, stop=stopped,
                        public=dict(id=identifier, label=folder.name, start_unix_ns=start_ns,
                                    attempt_unix_ns=attempt_ns,recording_started=bool(start_ns),
                                    failure_stage=('emg_qc' if summary.get('qc_mode')=='emg_only' and status=='qc_error' else 'preparation' if not start_ns else 'acquisition') if failed else None,
                                    duration_seconds=duration, qc=qc, status=status, stream_count=count))
                except ReplayError:
                    if self._excluded(folder):continue
                    # Corrupt metadata is retained as an explicit unavailable
                    # history entry rather than silently declared playable.
                    identifier = hashlib.sha256(str(folder.relative_to(self.root)).encode('utf-8')).hexdigest()[:24]
                    catalog[identifier] = dict(path=folder, summary={}, start={}, stop={}, corrupt=True,
                        public=dict(id=identifier,label=folder.name,start_unix_ns=0,duration_seconds=0,
                                    qc='UNKNOWN',status='corrupt',stream_count=0))
            self._catalog = catalog
            self._ordered = sorted(catalog.values(), key=lambda r:(r['public'].get('attempt_unix_ns',0), r['public']['label']), reverse=True)
            self._refreshed = time.monotonic()

    def sessions(self, offset=0, limit=20):
        if not 0 <= offset <= MAX_SESSIONS or not 1 <= limit <= 20:
            raise ReplayError('invalid_history_page')
        self._refresh()
        with self.lock:
            visible=[]
            for record in self._ordered:
                if self._excluded(record['path']):
                    self._catalog.pop(record['public']['id'],None)
                else:visible.append(record)
            self._ordered=visible
            rows = [dict(r['public']) for r in self._ordered[offset:offset+limit]]
            total = len(self._ordered)
        return dict(version=1,sessions=rows,total=total,
                    next_offset=offset+len(rows) if offset+len(rows)<total else None,
                    truncated=self._truncated)

    def _record(self, identifier):
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
            raise ReplayError('invalid_session_id')
        self._refresh()
        with self.lock:
            record = self._catalog.get(identifier)
        if record is None or self._excluded(record['path']):
            if record is not None:
                with self.lock:
                    self._catalog.pop(identifier,None)
                    self._ordered=[row for row in self._ordered if row['public']['id']!=identifier]
                self.release()
            raise ReplayError('recording_not_found', 404)
        self._safe(record['path'])
        # Recheck the stopped gate to prevent cached records being replayed if
        # someone has reopened/modified a historical directory externally.
        state = self._json(record['path']/'status.json')
        if state.get('recording_active') is True:
            raise ReplayError('recording_is_active', 409)
        return record

    def _files(self, record, stream):
        base = stream[:-4] if stream.endswith('_imu') and stream.startswith('emg_') else stream
        folder = self._safe(record['path']/base)
        chunks = sorted(folder.glob('chunk_*.npz'))
        if not chunks:
            chunks = sorted((folder/'raw_chunks').glob('chunk_*.npz'))
        if len(chunks)>MAX_CHUNKS:
            raise ReplayError('too_many_recording_chunks', 413)
        if chunks:
            return [self._safe(p, True) for p in chunks]
        merged = folder/(base+'.npz')
        return [self._safe(merged, True)] if merged.is_file() else []

    def _stream(self, record, name):
        folder, summary = record['path'], record['summary']
        base = name[:-4] if name.endswith('_imu') and name.startswith('emg_') else name
        detail = summary.get(base, {})
        detail = detail if isinstance(detail, dict) else {}
        duration = record['public']['duration_seconds']
        common = dict(id=name,label=LABELS[name],kind='video' if name in CAMERAS else 'signal',
                      duration_seconds=duration,available=False,error=None)
        if name in CAMERAS:
            frames = _int(detail.get('encoded', detail.get('frames')))
            fps = _number(detail.get('fps'), 20. if name.startswith('cam_ros_') else 30.)
            path = self._safe(folder/name/'frames.mp4')
            if not path.is_file():
                path = self._safe(folder/name/'video.mp4')
            probe = detail.get('probe', {}) if isinstance(detail.get('probe'),dict) else {}
            common.update(frames=frames,fps=fps,channels=0,units='video frames',
                          width=_int(probe.get('width')),height=_int(probe.get('height')),
                          recorded_frames=_int(detail.get('frames')),available=bool(frames and path.is_file() and path.stat().st_size),
                          duration_seconds=frames/fps if fps>0 else duration,
                          recording_duration_seconds=duration,time_basis='encoded CFR frame index / movie fps; capture timestamps remain in source files',
                          display_is_resized=True,playback_policy='complete_frames_slow_when_needed')
        else:
            imu = name.endswith('_imu') and name.startswith('emg_')
            samples = _int(detail.get('imu_frames' if imu else 'emg_frames' if name.startswith('emg_') else 'samples' if name=='eeg' else 'frames'))
            rate = 250. if name=='eeg' else 2000. if name in ('emg_left','emg_right') else 200.
            labels = ['CH'+str(i) for i in range(1,9)]
            units = 'raw signed 24-bit ADC counts'
            if name=='eeg':
                all_labels=detail.get('channel_labels', ['UNUSED','FC3','C3','O1','Oz','O2','C4','FC4'])
                if not isinstance(all_labels,(list,tuple)) or len(all_labels) not in (7,8):
                    raise ReplayError('invalid_eeg_channel_labels',422)
                labels=list(all_labels[1:] if len(all_labels)==8 else all_labels)
                labels=[str(label)[:32] for label in labels]
                units='uV'
            elif name=='board_imu' or imu:
                labels=['gyro_x','gyro_y','gyro_z','accel_x','accel_y','accel_z']
                units='rad/s | m/s^2' if name=='board_imu' else 'stored gyro | accel units'
            files=self._files(record,name)
            csv_path=self._safe(folder/'board_imu/raw.csv')
            common.update(samples=samples,sample_rate_hz=rate,fps=0,channels=len(labels),channel_labels=labels,units=units,
                          modes=['raw','filtered'] if name=='eeg' else ['raw'],
                          available=bool(samples and (files or name=='board_imu' and csv_path.is_file())))
        if not common['available']:
            common['error']='no_saved_data'
        return common

    def _qc_summary(self, record):
        report=self._json(record['path']/'qc_report.json') or self._json(record['path']/'emg_qc_report.json')
        if report:report=dict(report,preprocessing=self._json(record['path']/'emg_preprocessing.json'))
        # Native setup/acquisition failures bypass offline QC. Surface their
        # durable errors through the same read-only details endpoint.
        if not report and record['summary'].get('errors'):
            errors=list(dict.fromkeys(str(item)[:3000] for item in record['summary']['errors'][:100]))
            report=dict(level='ERROR',findings=[dict(level='ERROR',subject='session',
                check='acquisition',message=message) for message in errors])
        return summarize_qc(report)

    def session(self, identifier):
        record=self._record(identifier)
        result=dict(record['public']);result['streams']=[];result['warnings']=[]
        try:result['qc_details']=compact_qc(self._qc_summary(record))
        except ReplayError:result['qc_details']=dict(level='UNKNOWN',error_count=0,warn_count=0,total_findings=0,next_offset=None,findings=[],modalities={},unavailable=True)
        if record.get('corrupt'):
            result['warnings'].append('录制元数据损坏，不能作为可播放数据')
            result['impedance']={};return result
        if result.get('failure_stage')=='preparation':
            result['warnings'].append('准备失败：本次未开始正式录制；以下为采集错误，尚未运行离线质检。')
        for name in CAMERAS+SIGNALS:
            base=name[:-4] if name.endswith('_imu') and name.startswith('emg_') else name
            if base in record['summary'] or (record['path']/base).is_dir():
                try:result['streams'].append(self._stream(record,name))
                except ReplayError as error:
                    result['streams'].append(dict(id=name,label=LABELS[name],kind='video' if name in CAMERAS else 'signal',available=False,error=str(error)))
        try:
            imp=self._json(record['path']/'eeg/impedance_preflight.json')
            # Display measured fields only, without exposing absolute device paths.
            channels=[]
            for channel in imp.get('channels',[])[:32]:
                if not isinstance(channel,dict) or channel.get('enabled') is False:
                    continue
                value=_number(channel.get('value_kohm'),None)
                channels.append(dict(label=str(channel.get('label','CH'+str(channel.get('channel','?'))))[:32],
                    value_kohm=value,reason=str(channel.get('reason',''))[:64]))
            finite_count=sum(c['value_kohm'] is not None for c in channels)
            result['impedance']=dict(channels=channels,units='kOhm',
                summary=('已保存采前阻抗：%d 个通道有数值'%finite_count) if imp else '本段未保存阻抗检查结果',
                reviewed=imp.get('reviewed') is True,reviewed_at=_number(imp.get('reviewed_at'),None),
                completed_at=_number(imp.get('completed_at'),None),
                loff_off_confirmed=imp.get('loff_off_confirmed') is True,
                frontend_restored=imp.get('frontend_restored') is True)
        except ReplayError as error:
            result['impedance']={'error':str(error)}
        if not any(s.get('available') for s in result['streams']):
            result['warnings'].append('本段没有可回看的已保存样本或视频帧')
        result['warnings'].append('逐设备回看；显示缩略图或时域包络，原始文件只读，不代表设备间硬件同步')
        result['warnings'].extend('采集异常：'+str(value) for value in record['summary'].get('errors',[])[:100])
        present={item['id'] for item in result['streams']}
        for name in result['qc_details']['modalities']:
            if name in CAMERAS+SIGNALS and name not in present:
                result['streams'].append(dict(id=name,label=LABELS[name],kind='video' if name in CAMERAS else 'signal',available=False,error='QC 报告包含此模态问题，但没有可回看的数据'))
        for stream in result['streams']:
            key=stream['id'];base=key[:-4] if key.endswith('_imu') and key.startswith('emg_') else key
            stream['qc']=result['qc_details']['modalities'].get(key,result['qc_details']['modalities'].get(base,dict(level='INFO' if result['qc_details']['level']!='UNKNOWN' else 'UNKNOWN',error_count=0,warn_count=0,findings=[])))
        result['streams'].sort(key=lambda value:(-{'ERROR':2,'WARN':1}.get(value.get('qc',{}).get('level'),0),value['id']))
        return result

    def qc_page(self,identifier,offset=0,limit=50,stream=''):
        if not 0<=offset<=100000 or not 1<=limit<=50 or stream and stream not in CAMERAS+SIGNALS+('session','marker'):
            raise ReplayError('invalid_qc_page')
        record=self._record(identifier);value=self._qc_summary(record)
        base=stream[:-4] if stream.endswith('_imu') and stream.startswith('emg_') else stream
        rows=[item for item in value['findings'] if not stream or item['modality'] in (stream,base)]
        page=rows[offset:offset+limit]
        return dict(scope=value.get('scope','full'),error_count=sum(v['level']=='ERROR' for v in rows),warn_count=sum(v['level']=='WARN' for v in rows),
            total=len(rows),offset=offset,findings=page,unavailable=value['level']=='UNKNOWN',next_offset=offset+len(page) if offset+len(page)<len(rows) else None)

    def frames(self,identifier,stream,start,limit):
        if stream not in CAMERAS or not 0<=start<=1000000000 or not 1<=limit<=300:raise ReplayError('invalid_video_stream')
        self._guard();record=self._record(identifier);detail=self._stream(record,stream)
        if not detail['available'] or start>=detail['frames']:raise ReplayError('video_not_available',404)
        path=record['path']/stream/'frames.mp4'
        if not path.is_file():path=record['path']/stream/'video.mp4'
        self._safe(path,True)
        with self.lock:
            if self.video_decoder is None:
                from replay_video import VideoDecoder
                self.video_decoder=VideoDecoder()
            decoder=self.video_decoder
        for index in range(start,min(detail['frames'],start+limit)):
            self._guard()
            if self._json(record['path']/'status.json').get('recording_active') is True:raise ReplayError('recording_is_active',409)
            raw=decoder.get_frame(path,index,fps=detail['fps']);self._guard()
            yield raw,dict(frame=index,time=index/detail['fps'],frames=detail['frames'],fps=detail['fps'])

    def frame(self, identifier, stream, index):
        self._guard()
        if stream not in CAMERAS or not isinstance(index,int) or isinstance(index,bool) or index<0:
            raise ReplayError('invalid_video_frame')
        record=self._record(identifier)
        detail=self._stream(record,stream)
        if not detail['available']:
            raise ReplayError('video_not_available',404)
        if index>=detail['frames']:
            raise ReplayError('video_frame_out_of_range',416)
        path=record['path']/stream/'frames.mp4'
        if not path.is_file():path=record['path']/stream/'video.mp4'
        self._safe(path,True)
        with self.lock:
            if self.video_decoder is None:
                from replay_video import VideoDecoder
                self.video_decoder=VideoDecoder()
            decoder=self.video_decoder
        raw=decoder.get_frame(path,index,fps=detail['fps'])
        self._guard()
        if not isinstance(raw,bytes) or not raw:
            raise ReplayError('video_decoder_returned_empty_frame',422)
        return raw,dict(frame=index,time=index/detail['fps'] if detail['fps']>0 else None,frames=detail['frames'])

    def _time_spec(self, reader, record, stream, origin=None):
        keys=reader.keys
        start_mono=_number(record['start'].get('start_monotonic_ns'))
        start_unix=_number(record['start'].get('start_unix_ns',record['public']['start_unix_ns']))/1e9
        if stream=='eeg' and 'sample_indices' in keys:
            key='sample_indices';scale=1/250.;offset=origin if origin is not None else (float(reader.rows(key,0,1)[0]) if reader.describe(key)['shape'][0] else 0.)
            return key,scale,offset,'device sample index / 250 Hz, first saved sample as zero; hardware alignment unmeasured'
        prefix='imu' if stream.startswith('emg_') and stream.endswith('_imu') else 'emg'
        options=([('arrival_monotonic_ns',1e-9,start_mono),('ros_stamp_ns',1e-9,start_unix*1e9),('timestamps',1.,start_unix)]
                 if stream=='board_imu' else
                 [(prefix+'_monotonic_ns',1e-9,start_mono),(prefix+'_timestamps',1.,start_unix),(prefix+'_arrival_timestamps',1.,start_unix)])
        for key,scale,offset in options:
            if key in keys:return key,scale,offset,'recorded host/source timestamp relative to segment START; hardware alignment unmeasured'
        raise ReplayError('missing_signal_time_array',422)

    def _timed_bounds(self,path,record,stream,origin,deadline):
        with NpzReader(path,deadline=deadline) as r:
            key,scale,offset,basis=self._time_spec(r,record,stream,origin)
            n=r.describe(key)['shape'][0]
            if not n:return (float('inf'),float('-inf'))
            first=float(r.rows(key,0,1)[0]);last=float(r.rows(key,n-1,n)[0])
            return ((first-offset)*scale,(last-offset)*scale)

    def _data(self,r,stream,mode,begin,end):
        keys=r.keys
        if stream=='eeg':
            choices=('eeg_filtered_uv','eeg_filtered_data') if mode=='filtered' else ('eeg_uv','eeg_raw_uv','eeg_data')
            key=next((k for k in choices if k in keys),None)
            if key is None:raise ReplayError('requested_eeg_mode_not_saved',422)
            y=r.rows(key,begin,end)
            if y.ndim!=2 or y.shape[1] not in (7,8):raise ReplayError('invalid_eeg_channels',422)
            if y.shape[1]==8:y=y[:,1:]
            valid=r.rows('valid',begin,end).astype(bool) if 'valid' in keys else np.ones(end-begin,dtype=bool)
            indices=r.rows('sample_indices',begin,end) if 'sample_indices' in keys else None
        elif stream in ('emg_left','emg_right'):
            y=r.rows('emg_data',begin,end);valid=np.ones(end-begin,dtype=bool);indices=None
        elif stream.startswith('emg_') and stream.endswith('_imu'):
            y=np.concatenate((r.rows('imu_gyro',begin,end),r.rows('imu_accel',begin,end)),axis=1)
            valid=np.ones(end-begin,dtype=bool);indices=None
        else:
            y=np.concatenate((r.rows('angular_velocity',begin,end),r.rows('linear_acceleration',begin,end)),axis=1)
            valid=np.ones(end-begin,dtype=bool);indices=None
        return y,valid,indices

    def signal(self,identifier,stream,start=0.,duration=2.,width=600,mode='raw'):
        self._guard()
        deadline=time.monotonic()+6.
        start,duration=_number(start,-1),_number(duration,-1)
        if (stream not in SIGNALS or start<0 or not 0<duration<=30 or
                not isinstance(width,int) or isinstance(width,bool) or not 1<=width<=800 or mode not in ('raw','filtered')):
            raise ReplayError('invalid_signal_window')
        record=self._record(identifier);detail=self._stream(record,stream)
        if not detail['available']:raise ReplayError('signal_not_available',404)
        if mode=='filtered' and stream!='eeg':raise ReplayError('filtered_mode_only_for_eeg')
        files=self._files(record,stream);channels=detail['channels']
        low=np.full((width,channels),np.inf);high=np.full((width,channels),-np.inf)
        count=invalid=missing=0;previous=None;basis='';warnings=[]
        origin=None
        if stream=='eeg' and files:
            for path in files:
                if time.monotonic()>deadline:raise ReplayError('recording_read_timeout',503)
                with NpzReader(path,deadline=deadline) as r:
                    if 'sample_indices' in r.keys and r.describe('sample_indices')['shape'][0]:
                        origin=float(r.rows('sample_indices',0,1)[0]);break
        def add(t,y,valid,indices=None):
            nonlocal count,invalid,missing,previous
            if y.ndim!=2 or y.shape[1]!=channels or len(t)!=len(y) or len(valid)!=len(y):raise ReplayError('signal_array_shape_mismatch',422)
            if count+len(y)>MAX_WINDOW_SAMPLES:raise ReplayError('signal_window_has_too_many_samples',413)
            if len(t)>1 and np.any(np.diff(t)<0):raise ReplayError('signal_timestamps_not_monotonic',422)
            selected=(t>=start)&(t<start+duration)&np.isfinite(t)
            t,y,valid=t[selected],y[selected],valid[selected]
            count+=len(t);invalid+=int((~valid|~np.isfinite(y).all(axis=1)).sum())
            if indices is not None:
                indices=indices[selected].astype(np.int64)
                if len(indices):
                    delta=np.diff(np.r_[previous if previous is not None else indices[0]-1,indices])
                    if np.any(delta<=0):raise ReplayError('eeg_indices_not_increasing',422)
                    missing+=int(np.maximum(delta-1,0).sum());previous=int(indices[-1])
            bins=np.floor((t-start)/duration*width).astype(np.int64)
            for ch in range(channels):
                ok=valid&np.isfinite(y[:,ch])&(bins>=0)&(bins<width)
                if np.any(ok):
                    np.minimum.at(low[:,ch],bins[ok],y[ok,ch])
                    np.maximum.at(high[:,ch],bins[ok],y[ok,ch])
        if files:
            # Binary search sorted chunk end times; late seeks do not scan every
            # preceding sample or read an entire consolidated recording.
            left,right=0,len(files)
            while left<right:
                if time.monotonic()>deadline:raise ReplayError('recording_read_timeout',503)
                mid=(left+right)//2
                if self._timed_bounds(files[mid],record,stream,origin,deadline)[1]<start:left=mid+1
                else:right=mid
            visited=0
            for path in files[left:]:
                if time.monotonic()>deadline:raise ReplayError('recording_read_timeout',503)
                visited+=1
                if visited>256:raise ReplayError('too_many_chunks_for_window',413)
                first,last=self._timed_bounds(path,record,stream,origin,deadline)
                if first>=start+duration:break
                if last<start:continue
                with NpzReader(path,deadline=deadline) as r:
                    key,scale,offset,basis=self._time_spec(r,record,stream,origin)
                    n=r.describe(key)['shape'][0]
                    def search(target):
                        lo,hi=0,n
                        while lo<hi:
                            mid=(lo+hi)//2
                            value=(float(r.rows(key,mid,mid+1)[0])-offset)*scale
                            if value<target:lo=mid+1
                            else:hi=mid
                        return lo
                    a,b=search(start),search(start+duration)
                    if b>a:
                        times=(r.rows(key,a,b).astype(np.float64)-offset)*scale
                        y,valid,indices=self._data(r,stream,mode,a,b)
                        add(times,y,valid,indices)
        elif stream=='board_imu':
            path=self._safe(record['path']/'board_imu/raw.csv',True)
            with path.open(newline='',encoding='utf-8-sig') as h:
                reader=csv.DictReader(_bounded_csv_lines(h,deadline));rows=[];times=[]
                for number,row in enumerate(reader):
                    if number%1000==0 and time.monotonic()>deadline:raise ReplayError('csv_read_timeout',503)
                    if number>=MAX_CSV_ROWS:raise ReplayError('csv_scan_limit_reached',413)
                    t=(_number(row.get('arrival_monotonic_ns'))-_number(record['start'].get('start_monotonic_ns')))/1e9
                    if t>=start+duration:break
                    if t<start:continue
                    times.append(t);rows.append([float(row[prefix+axis]) for prefix in ('angular_velocity_','linear_acceleration_') for axis in 'xyz'])
                if rows:add(np.asarray(times),np.asarray(rows),np.ones(len(rows),dtype=bool))
            basis='recorded host arrival relative to segment START; hardware alignment unmeasured'
        rows=[]
        for i in range(width):
            bounds=[[float(low[i,ch]),float(high[i,ch])] if math.isfinite(low[i,ch]) and math.isfinite(high[i,ch]) else [None,None] for ch in range(channels)]
            rows.append([start+(i+.5)*duration/width,bounds])
        if not count:warnings.append('所选时间窗没有已保存的样本')
        if invalid:warnings.append('含无效样本；无效点不绘制，不替换为零')
        if missing:warnings.append('设备索引存在缺号；原始缺口保留')
        base=stream[:-4] if stream.startswith('emg_') and stream.endswith('_imu') else stream
        stored_summary=record['summary'].get(base,{})
        stored_gaps=stored_summary.get('sequence_gaps') if isinstance(stored_summary,dict) else None
        number_gaps=_number(stored_gaps,None) if isinstance(stored_gaps,(int,float)) and not isinstance(stored_gaps,bool) else None
        sequence_gaps=int(number_gaps) if number_gaps is not None and 0<=number_gaps<=1000000000 and number_gaps==math.floor(number_gaps) else None
        if sequence_gaps:
            warnings.append('本段原始摘要报告串口序号缺口：'+str(sequence_gaps)+'；当前窗口未独立定位这些缺口')
        return dict(kind='signal',stream=stream,label=detail['label'],units=detail['units'],channel_labels=detail['channel_labels'],
            rows=rows,start=start,duration=duration,sample_rate_hz=detail['sample_rate_hz'],total_samples=detail['samples'],
            window_samples=count,invalid_samples=invalid,missing_samples=missing,mode=mode,time_basis=basis,
            missing_samples_measured=(stream=='eeg'),recording_sequence_gaps=sequence_gaps,
            display_aggregation='temporal min/max; all finite valid samples in requested window contribute',warnings=warnings)

    def release(self):
        with self.lock:
            decoder=self.video_decoder
            self.video_decoder=None
        if decoder is not None:
            try:decoder.close()
            except Exception:
                # Retain ownership if cleanup failed so a later watchdog/release
                # can retry; never claim a worker has been released prematurely.
                with self.lock:
                    if self.video_decoder is None:self.video_decoder=decoder
                return dict(released=False,error='decoder_release_failed')
        return dict(released=True)

    def health(self):
        with self.lock:
            decoder=self.video_decoder
            value=dict(enabled=True,cached_sessions=len(self._ordered),decoder_initialized=decoder is not None)
        if decoder is not None:
            try:value['decoder']=decoder.health()
            except Exception:value['decoder']={'error':'decoder_health_unavailable'}
        return value

    def close(self):
        self.release()


def _one(query,key,default=None):
    values=query.get(key)
    if values is None:
        if default is None:raise ReplayError('missing_parameter:'+key)
        return default
    if len(values)!=1 or len(values[0])>128:raise ReplayError('invalid_parameter:'+key)
    return values[0]


def _error_body(code,error):
    messages={400:'回看参数无效，请重新选择采集段。',401:'访问验证失败，请检查终端连接。',
        403:'当前连接没有读取本机记录的权限。',404:'没有找到该采集段或设备数据。',
        409:'正在采集或质量检验，请结束后再回看。',413:'所选范围过大，请缩小时间范围。',
        414:'请求过长，请重新选择采集段。',416:'已到达视频边界，请选择有效帧。',
        422:'这份记录不完整或无法读取，请结合本段检验结果检查。',
        503:'回看暂不可用，请稍后重试。'}
    return dict(error=str(error),message=messages.get(code,'无法读取这份记录，请稍后重试。'))


def handle_replay(handler):
    """Return True only for replay routes. Root invokes after token auth."""
    url=urlsplit(handler.path)
    if not url.path.startswith('/replay'):return False
    try:
        if len(handler.path)>1024:raise ReplayError('request_too_long',414)
        token=getattr(handler.server,'token','')
        if token:
            supplied=handler.headers.get('X-Access-Token','')
            try:authorised=len(supplied)<=511 and hmac.compare_digest(token.encode('ascii'),supplied.encode('ascii'))
            except UnicodeError:authorised=False
            if not authorised:raise ReplayError('unauthorized',401)
        elif handler.client_address[0] not in READ_PEERS:raise ReplayError('peer_forbidden',403)
        if handler.headers.get('Content-Length','0')!='0' or handler.headers.get('Transfer-Encoding'):
            raise ReplayError('unexpected_request_body')
        api=getattr(handler.server,'replay',None)
        if api is None:raise ReplayError('replay_unavailable',503)
        query=parse_qs(url.query,keep_blank_values=True)
        allowed={
            '/replay/sessions':{'offset','limit'},'/replay/session':{'id'},'/replay/qc':{'id','offset','limit','stream'},
            '/replay/frame.jpg':{'session','stream','frame'},
            '/replay/stream':{'session','stream','start','limit'},
            '/replay/signal':{'session','stream','start','duration','width','mode'},'/replay/release':set()}
        if url.path not in allowed:raise ReplayError('replay_route_not_found',404)
        if set(query)-allowed[url.path]:raise ReplayError('unknown_parameter')
        if url.path=='/replay/release':value=api.release()
        elif url.path=='/replay/sessions':
            value=api.sessions(int(_one(query,'offset','0')),int(_one(query,'limit','20')))
        elif url.path=='/replay/session':value=api.session(_one(query,'id'))
        elif url.path=='/replay/qc':value=api.qc_page(_one(query,'id'),int(_one(query,'offset','0')),int(_one(query,'limit','50')),_one(query,'stream',''))
        elif url.path=='/replay/stream':
            from frame_stream import packet,begin
            if not handler.server.stream_slot.acquire(False):raise ReplayError('display_stream_busy',429)
            begun=False
            try:
                frames=iter(api.frames(_one(query,'session'),_one(query,'stream'),int(_one(query,'start','0')),int(_one(query,'limit','120'))))
                first=next(frames);begin(handler);begun=True;deadline=time.monotonic()+20
                raw,meta=first;handler.wfile.write(packet(meta,raw))
                for raw,meta in frames:
                    if time.monotonic()>deadline:break
                    handler.wfile.write(packet(meta,raw))
            except (OSError,StopIteration):pass
            except Exception:
                if not begun:raise
            finally:handler.server.stream_slot.release()
            return True
        elif url.path=='/replay/frame.jpg':
            raw,meta=api.frame(_one(query,'session'),_one(query,'stream'),int(_one(query,'frame')))
            handler.send(200,raw,'image/jpeg',frame_id='replay:'+str(meta['frame']))
            return True
        else:
            value=api.signal(_one(query,'session'),_one(query,'stream'),float(_one(query,'start','0')),
                float(_one(query,'duration','2')),int(_one(query,'width','600')),_one(query,'mode','raw'))
        handler.send(200,value)
    except ReplayError as error:handler.send(error.status,_error_body(error.status,error))
    except (ValueError,OverflowError,TypeError):handler.send(400,_error_body(400,'invalid_replay_parameters'))
    except Exception as error:
        # No tracebacks, filesystem paths or participant data cross the boundary.
        code=getattr(error,'status',422)
        handler.send(code,_error_body(code,'recording_read_failed:'+type(error).__name__))
    return True
