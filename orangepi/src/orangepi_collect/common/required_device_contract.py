"""Frozen profile-level hardware contract for capture START and QC."""
from pathlib import Path
import math
import stat

CONTRACT_VERSION='required-devices-v1'
MAX_READY_AGE_SECONDS=1.5
HEADBAND=tuple('cam_ros_'+name for name in ('left','right','bleft','bright'))
WRISTS=('cam_left_wrist','cam_right_wrist')
EMG=('emg_left','emg_right')
BOARD_IMU=('board_imu',)


def _mode(config,mode=None):
    config=config if isinstance(config,dict) else {}
    value=mode or config.get('collection_mode') or config.get('capture_profile') or 'emg'
    return value if value in ('emg','eeg') else None


def required_names(config,mode=None):
    """Return profile-required stream names, independent of scan results."""
    config=config if isinstance(config,dict) else {}
    mode=_mode(config,mode)
    names=list(HEADBAND+WRISTS+EMG+BOARD_IMU)
    if mode=='eeg':names.append('eeg')
    return tuple(names)


def configuration_errors(config,mode=None,legacy=False):
    if not isinstance(config,dict):return ['设备配置不存在']
    mode=_mode(config,mode)
    if mode is None:return ['采集模式无效']
    missing=[]
    cameras=config.get('cameras') if isinstance(config.get('cameras'),dict) else {}
    for name in ('left','right','bleft','bright'):
        row=cameras.get(name)
        source=(row.get('camd') or row.get('topic')) if isinstance(row,dict) else None
        if not isinstance(source,str) or not source.strip():missing.append('cam_ros_'+name)
    wrists=config.get('wrist_cameras') if isinstance(config.get('wrist_cameras'),dict) else {}
    for name in WRISTS:
        row=wrists.get(name)
        if not isinstance(row,dict) or not isinstance(row.get('device'),str) or not row['device'].strip():missing.append(name)
    emg=config.get('emg') if isinstance(config.get('emg'),dict) else {}
    for name in EMG:
        row=emg.get(name)
        if not isinstance(row,dict) or not isinstance(row.get('port'),str) or not row['port'].strip():missing.append(name)
    board=config.get('board_imu')
    board_source=(board.get('device') or board.get('topic')) if isinstance(board,dict) and legacy else (board.get('device') if isinstance(board,dict) else None)
    if not isinstance(board_source,str) or not board_source.strip():missing.append('board_imu')
    if mode=='eeg':
        eeg=config.get('eeg')
        if not isinstance(eeg,dict) or eeg.get('enabled') is not True:missing.append('eeg')
    return ['必需设备配置缺失：'+','.join(missing)] if missing else []


def build_contract(config,selected=None,mode=None,legacy=False):
    mode=_mode(config,mode)
    if mode is None:raise ValueError('采集模式无效，无法冻结必需设备清单')
    if isinstance(config,dict) and config.get('collection_mode') not in (None,mode):
        raise ValueError('冻结采集模式与设备配置不一致')
    errors=configuration_errors(config,mode,legacy=legacy)
    if errors:raise ValueError(errors[0])
    required=list(required_names(config,mode))
    selected=list(selected if selected is not None else required)
    if (not selected or any(not isinstance(name,str) or not name for name in selected)
            or len(selected)!=len(set(selected))):raise ValueError('冻结设备清单为空或包含重复项')
    missing=sorted(set(required)-set(selected))
    if missing:raise ValueError('本段缺少必需设备：'+','.join(missing))
    allowed=set(HEADBAND+WRISTS+EMG+BOARD_IMU+('eeg',))
    extra=sorted(set(selected)-allowed)
    if extra:raise ValueError('冻结设备清单含未知设备：'+','.join(extra))
    return dict(version=CONTRACT_VERSION,mode=mode,required_devices=required,selected_devices=selected)


def validate_scan(contract,rows):
    grouped={}
    for row in rows if isinstance(rows,(list,tuple)) else ():
        if isinstance(row,dict) and isinstance(row.get('name'),str):
            grouped.setdefault(row['name'],[]).append(row)
    missing=[]
    for name in contract.get('required_devices',[]):
        values=grouped.get(name,[])
        if (len(values)!=1 or values[0].get('connected') is not True
                or values[0].get('status')!='ready'):
            missing.append(name)
    return missing


def validate_frozen(config,selection):
    if not isinstance(selection,dict):return None,['冻结设备选择无效']
    selected=selection.get('selected')
    if (not isinstance(selected,list) or not selected
            or any(not isinstance(name,str) or not name for name in selected)
            or len(selected)!=len(set(selected))):
        return None,['冻结设备清单缺失、为空或含无效项']
    try:contract=build_contract(config,selected,selection.get('collection_mode'))
    except (ValueError,TypeError) as error:return None,[str(error)]
    frozen=selection.get('required_device_contract')
    if not isinstance(frozen,dict):return None,['缺少必需设备合同，不能默认视为完整采集']
    if (frozen.get('version')!=CONTRACT_VERSION or frozen.get('mode')!=contract['mode']
            or frozen.get('required_devices')!=contract['required_devices']
            or frozen.get('selected_devices')!=contract['selected_devices']):
        return None,['本段必需设备合同与冻结配置不一致']
    return contract,[]


def resolve_selection(config,selection):
    """Resolve current contracts strictly; derive and label legacy contracts."""
    if not isinstance(selection,dict):return None,['冻结设备选择无效'],False
    if 'required_device_contract' in selection:
        contract,errors=validate_frozen(config,selection)
        return contract,errors,False
    selected=selection.get('selected')
    if (not isinstance(selected,list) or not selected
            or any(not isinstance(name,str) or not name for name in selected)
            or len(selected)!=len(set(selected))):
        return None,['旧会话冻结设备选择缺失或无效'],True
    try:contract=build_contract(config,selection.get('selected'),selection.get('collection_mode'),legacy=True)
    except (ValueError,TypeError) as error:return None,[str(error)],True
    return contract,[],True


def execution_missing(contract,cameras,with_emg,with_board,emg_names,wrist_names,with_eeg):
    actual=set('cam_ros_'+name for name in cameras)|set(wrist_names or ())
    if with_emg:actual.update(emg_names if emg_names is not None else EMG)
    if with_board:actual.add('board_imu')
    if with_eeg:actual.add('eeg')
    return sorted(set(contract.get('required_devices',()))-actual)


def data_errors(root,summary,contract,legacy=False):
    """Check frozen required streams from saved evidence, not current hardware."""
    root=Path(root);missing=[]
    if not isinstance(summary,dict):return ['采集摘要缺失']
    if summary.get('status')!='success' or summary.get('errors'):
        return ['采集摘要未证明设备收尾成功']
    has_new_evidence=('required_device_contract_version' in summary
        or 'required_device_start' in summary or 'required_device_stop' in summary)
    if legacy and has_new_evidence:return ['新版在线证明存在但冻结设备合同缺失']
    required=set(contract.get('required_devices',()))
    if not legacy:
        frozen=summary.get('required_device_contract')
        if (summary.get('required_device_contract_version')!=CONTRACT_VERSION
                or summary.get('required_devices')!=contract.get('required_devices')
                or not isinstance(frozen,dict) or frozen.get('version')!=CONTRACT_VERSION
                or frozen.get('mode')!=contract.get('mode')
                or frozen.get('required_devices')!=contract.get('required_devices')
                or frozen.get('selected_devices')!=contract.get('selected_devices')):
            return ['采集摘要中的冻结必需设备合同不一致']
    for key in (() if legacy else ('required_device_start','required_device_stop')):
        evidence=summary.get(key)
        rows=evidence.get('devices') if isinstance(evidence,dict) else None
        if (not isinstance(rows,dict) or not required.issubset(rows)
                or evidence.get('ready') is not True or evidence.get('missing')):
            return ['缺少全部必需设备的'+('START' if key.endswith('start') else 'STOP')+'实时就绪证据']
        for name in required:
            row=rows.get(name)
            if (not isinstance(row,dict) or row.get('ready') is not True or row.get('fresh') is not True
                    or not _finite_number(row.get('age_seconds')) or row['age_seconds']<0
                    or row['age_seconds']>MAX_READY_AGE_SECONDS
                    or not _finite_number(row.get('last_seen_monotonic'))
                    or row['last_seen_monotonic']<=0):
                missing.append(name+':'+('START' if key.endswith('start') else 'STOP')+'时未保持在线')
    if missing:return sorted(set(missing))
    if not legacy:
        if (type(summary.get('start_unix_ns')) is not int or type(summary.get('stop_unix_ns')) is not int
                or summary['start_unix_ns']<=0 or summary['stop_unix_ns']<=summary['start_unix_ns']
                or type(summary.get('start_monotonic_ns')) is not int
                or type(summary.get('stop_monotonic_ns')) is not int
                or summary['start_monotonic_ns']<=0 or summary['stop_monotonic_ns']<=summary['start_monotonic_ns']):
            return ['本机START/STOP时间无效']
        try:
            start=_read_evidence(root/'start_gate.json');stop=_read_evidence(root/'stop_gate.json')
            if (start.get('required_device_contract_version')!=CONTRACT_VERSION
                    or start.get('required_devices')!=contract.get('required_devices')
                    or start.get('required_device_readiness')!=summary['required_device_start']['devices']
                    or type(start.get('start_unix_ns')) is not int
                    or start['start_unix_ns']!=summary.get('start_unix_ns')
                    or type(start.get('start_monotonic_ns')) is not int
                    or start['start_monotonic_ns']!=summary.get('start_monotonic_ns')
                    or stop.get('recording_active') is not False
                    or type(stop.get('stop_unix_ns')) is not int
                    or stop['stop_unix_ns']!=summary.get('stop_unix_ns')
                    or type(stop.get('stop_monotonic_ns')) is not int
                    or stop['stop_monotonic_ns']!=summary.get('stop_monotonic_ns')):
                return ['本机START/STOP门限凭据与采集摘要不一致']
        except (OSError,ValueError,TypeError,UnicodeError):
            return ['本机START/STOP门限凭据缺失或无效']
    def file_ready(path):
        try:
            info=path.lstat()
            return stat.S_ISREG(info.st_mode) and info.st_size>0
        except OSError:return False
    def positive(value):return _finite_number(value) and value>0
    for name in contract.get('required_devices',[]):
        detail=summary.get(name);directory=root/name
        try:directory_ok=stat.S_ISDIR(directory.lstat().st_mode)
        except OSError:directory_ok=False
        if not isinstance(detail,dict) or not directory_ok:
            missing.append(name+':缺少数据目录或摘要');continue
        if name in HEADBAND+WRISTS:
            valid=positive(detail.get('frames')) and file_ready(directory/'frames.mp4') and file_ready(directory/'timestamps.csv')
        elif name in EMG:
            # Full QC standardizes the raw chunks into one canonical array and
            # archives the source chunks below raw_chunks/. The canonical file
            # is what the framework checkers actually decoded.
            valid=positive(detail.get('emg_frames')) and file_ready(directory/(name+'.npz'))
        elif name=='board_imu':
            valid=positive(detail.get('frames')) and file_ready(directory/'raw.csv')
        elif name=='eeg':
            valid=positive(detail.get('samples')) and any(file_ready(p) for p in directory.glob('*.npz'))
        else:valid=False
        if not valid:missing.append(name+':数据为空或不完整')
    return missing


def _finite_number(value):
    if type(value) not in (int,float):return False
    try:return math.isfinite(value)
    except (OverflowError,TypeError):return False


def _read_evidence(path,limit=1024*1024):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('invalid evidence file')
    def reject(value):raise ValueError('non-finite JSON value')
    value=__import__('json').loads(path.read_text(encoding='utf-8'),parse_constant=reject)
    if not isinstance(value,dict):raise ValueError('evidence JSON is not an object')
    return value


def _report_series_matches(stream,label,total,window,match_total=True):
    series=stream.get('series') if isinstance(stream,dict) else None
    row=series.get(label) if isinstance(series,dict) else None
    if (not isinstance(row,dict) or type(total) is not int or type(row.get('n')) is not int
            or row['n']<1):return False
    outside=row.get('n_outside_window',0)
    if type(outside) is not int or outside<0:return False
    if match_total and row['n']+outside!=total:return False
    if (not _finite_number(row.get('t0')) or not _finite_number(row.get('t1'))
            or row['t1']<row['t0'] or row['t0']<window['t0']-1e-6
            or row['t1']>window['t1']+1e-6):return False
    if 'duration' in row and (not _finite_number(row['duration'])
            or not math.isclose(row['duration'],row['t1']-row['t0'],rel_tol=0,abs_tol=1e-6)):
        return False
    return True


def _frame_decode_matches(stream):
    stats=stream.get('stats') if isinstance(stream,dict) else None
    value=stats.get('FrameCountMatch') if isinstance(stats,dict) else None
    series=stream.get('series') if isinstance(stream,dict) else None
    frames=series.get('frames') if isinstance(series,dict) else None
    if not isinstance(value,dict) or not isinstance(frames,dict):return False
    outside=frames.get('n_outside_window',0)
    if (value.get('file')!='frames.mp4' or type(value.get('n_frames')) is not int
            or type(value.get('n_timestamps')) is not int or type(value.get('n_compared')) is not int
            or type(value.get('n_window')) is not int or type(frames.get('n')) is not int
            or type(outside) is not int):return False
    # Window-mode FrameCountMatch intentionally permits pre-window warmup
    # frames; only its decoded in-window coverage is compared with timestamps.
    return (value['n_frames']>0 and value['n_timestamps']==frames['n']
            and value['n_compared']==value['n_window']==frames['n'])


def quality_errors(root,summary,contract,report,legacy=False):
    """Require a full report plus per-stream evidence of actual saved data.

    A top-level INFO/WARN is only a verdict. It is not proof that every frozen
    stream was read, decoded and checked. Legacy reports may use their original
    checker statistics where the newer Pi evidence fields did not exist.
    """
    errors=[]
    if not isinstance(report,dict):return ['完整质检报告缺失']
    if report.get('scope','full')!='full':return ['本段只有局部质检报告']
    if report.get('level') not in ('INFO','WARN'):return ['完整质检未通过或等级无效']
    rows=report.get('streams')
    required=set(contract.get('required_devices',()))
    if not isinstance(rows,dict) or not required.issubset(rows):
        return ['完整质检报告未覆盖所有必需数据流']
    window=report.get('window')
    valid_window=(isinstance(window,dict) and _finite_number(window.get('t0'))
        and _finite_number(window.get('t1')) and window['t1']>window['t0'])
    if not valid_window:return ['完整质检缺少有效录制窗口']
    for name in sorted(required):
        stream=rows.get(name)
        if not isinstance(stream,dict) or stream.get('level') not in ('INFO','WARN'):
            errors.append(name+':单流质检未通过或缺失');continue
        stats=stream.get('stats')
        if not isinstance(stats,dict):
            errors.append(name+':缺少本机逐流证据');continue
        if name in HEADBAND+WRISTS:
            detail=summary.get(name)
            total=detail.get('frames') if isinstance(detail,dict) else None
            if not isinstance(total,int) or isinstance(total,bool) or total<=0:
                errors.append(name+':采集摘要没有有效帧数')
            elif (not _frame_decode_matches(stream)
                    or not _report_series_matches(stream,'frames',total,window,match_total=False)):
                errors.append(name+':完整视频解码或时间戳计数与采集摘要不一致')
        elif name in EMG:
            detail=summary.get(name) if isinstance(summary.get(name),dict) else {}
            emg_count=detail.get('emg_frames');imu_count=detail.get('imu_frames')
            if (not _report_series_matches(stream,'emg',emg_count,window)
                    or not _report_series_matches(stream,'imu',imu_count,window)):
                errors.append(name+':EMG/IMU时间戳全流检查计数与采集摘要不一致')
        elif name=='board_imu':
            detail=summary.get(name);total=detail.get('frames') if isinstance(detail,dict) else None
            if not _report_series_matches(stream,'imu',total,window):
                errors.append(name+':IMU时间戳全流检查计数与采集摘要不一致')
        elif name=='eeg':
            eeg=stats.get('OmniEegIntegrity')
            detail=summary.get('eeg') if isinstance(summary.get('eeg'),dict) else {}
            count=detail.get('samples')
            if (not isinstance(eeg,dict) or type(count) is not int or type(eeg.get('samples')) is not int
                    or eeg['samples']!=count or type(eeg.get('missing_samples')) is not int
                    or eeg['missing_samples']<0 or type(eeg.get('invalid_samples')) is not int
                    or eeg['invalid_samples']<0):
                errors.append(name+':原始脑电全流检查计数与采集摘要不一致')
    start=summary.get('start_unix_ns');stop=summary.get('stop_unix_ns')
    if type(start) is int and type(stop) is int:
        if (not math.isclose(window['t0'],start/1e9,rel_tol=0,abs_tol=1e-6)
                or not math.isclose(window['t1'],stop/1e9,rel_tol=0,abs_tol=1e-6)):
            errors.append('质检窗口与本机START/STOP时间不一致')
    return errors


def qualification_errors(root,summary,contract,report,legacy=False):
    """One shared pass/fail decision for capture commit, task accounting and upload."""
    try:
        errors=data_errors(root,summary,contract,legacy=legacy)
        errors.extend(quality_errors(root,summary,contract,report,legacy=legacy))
        return list(dict.fromkeys(errors))
    except Exception as error:
        # Malformed JSON must produce a negative verdict, never an exception
        # that callers accidentally treat as an unavailable or passed check.
        return ['完整性证据结构无效：'+type(error).__name__]


def data_warnings(summary,legacy=False):
    if not legacy:return []
    if isinstance(summary,dict) and not any(key in summary for key in ('required_device_start','required_device_stop','required_device_contract_version')):
        return ['legacy_online_evidence_unavailable']
    return []
