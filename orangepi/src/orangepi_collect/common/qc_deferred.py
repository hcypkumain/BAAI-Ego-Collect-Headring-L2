"""Durable pending-QC state. Saving a capture does not imply a QC pass."""
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
import tempfile
import time
import uuid
import yaml
from qc_explanations import emg_error_messages

MODES=('immediate','emg_only','deferred')

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        directory=os.open(str(path.parent),os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)

def load(base):
    path=Path(base)/'configs/qc_execution.json'
    if not path.exists():return {'version':1,'mode':'immediate'}
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or value.get('version')!=1 or value.get('mode') not in MODES:
        raise ValueError('质检执行模式配置无效')
    return value

def save(base,mode):
    if mode not in MODES:raise ValueError('请选择采集后质检或待主机质检')
    value={'version':1,'mode':mode};atomic(Path(base)/'configs/qc_execution.json',value);return value

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def _read_json(path,limit=16*1024*1024):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('invalid regular JSON evidence: '+path.name)
    def reject(value):raise ValueError('non-finite JSON value')
    value=json.loads(path.read_text(encoding='utf-8'),parse_constant=reject)
    if not isinstance(value,dict):raise ValueError('JSON evidence must be an object: '+path.name)
    return value

def save_pending(path,summary,base,project,emg_report=None,eeg_report=None):
    from orangepi_collect.storage.durability import sync_session
    from orangepi_collect.storage.ledger import atomic_yaml
    from required_device_contract import resolve_selection
    from capture_qc_receipt import input_signature,portable_input_signature
    path=Path(path)
    if summary.get('status')!='success' or summary.get('errors'):
        raise ValueError('Only a successfully closed native capture may await QC')
    stop=json.loads((path/'stop_gate.json').read_text())
    if stop.get('recording_active') is not False or not (path/'start_gate.json').is_file():
        raise ValueError('Capture START/STOP confirmation is missing')
    raw=json.loads((path/'meta.json').read_text(encoding='utf-8'))
    selection=json.loads((path/'device_selection.json').read_text(encoding='utf-8'))
    contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
    if contract_errors or contract is None:
        raise ValueError('Pending QC requires a valid frozen required-device selection')
    pending=dict(version=1,request_id=uuid.uuid4().hex,session_id=path.name,state='pending',
                 native_sha256=digest(path/'native_capture_summary.json'),
                 reference_sha256=raw.get('reference_sha256'),
                 input_signature=portable_input_signature(path,contract,require_marker=False),
                 local_input_signature=input_signature(path,contract,require_marker=False),
                 legacy_source=legacy,created_at=time.time())
    failed=any(r is not None and r['level']=='ERROR' for r in (emg_report,eeg_report))
    mode='emg_only' if emg_report is not None or eeg_report is not None else 'deferred'
    status='qc_error' if failed else 'pending_qc'
    summary=dict(summary,status=status,capture_status='success',qc_state='partial_error' if failed else 'pending',
                 qc_level='ERROR' if failed else 'PENDING',qc_mode=mode,recording_active=False,qc_request_id=pending['request_id'])
    if emg_report is not None:
        atomic(path/'emg_qc_report.json',emg_report)
        summary['emg_qc_level']=emg_report['level']
        if failed:
            preprocessing={}
            try:preprocessing=json.loads((path/'emg_preprocessing.json').read_text())
            except (OSError,ValueError):pass
            summary['errors']=list(summary.get('errors',[]))+emg_error_messages(emg_report,preprocessing)
    if eeg_report is not None:
        atomic(path/'eeg_qc_report.json',eeg_report);summary['eeg_qc_level']=eeg_report['level']
        if eeg_report['level']=='ERROR':
            reasons=[f.get('message','脑电检查未通过') for s in [eeg_report]+list(eeg_report.get('streams',{}).values()) for f in s.get('findings',[]) if f.get('level')=='ERROR']
            summary['errors']=list(summary.get('errors',[]))+['脑电检查：'+x for x in reasons]
    summary['local_qc_scopes']=[name for name,report in [('emg',emg_report),('eeg',eeg_report)] if report is not None]
    template=Path(project)/'configs/meta.yaml'
    meta=yaml.safe_load(template.read_text(encoding='utf-8')) if template.exists() else {}
    meta=dict(meta or {},**raw)
    duration=max(0,summary.get('stop_unix_ns',0)-summary.get('start_unix_ns',0))/1e9
    meta.update(pi=raw,session_dir=str(path),status=status,qc_state=summary['qc_state'],qc_level=summary['qc_level'],
                qc_mode=mode,duration=duration,start_time=summary.get('start_unix_ns',0)/1e9,
                retention_state='kept_pending_qc',reference_state='pending_qc',upload_state='pending_qc')
    # A previous receipt cannot qualify a newly pending run. Keep its artifacts
    # for diagnosis, but remove its stale signature before publishing pending.
    try:(path/'capture_qc_receipt.json').unlink()
    except FileNotFoundError:pass
    atomic(path/'qc_pending.json',pending)
    atomic(path/'capture_summary.json',summary);atomic_yaml(path/'meta.yaml',meta)
    atomic(path/'status.json',dict(state='saving',pid=os.getpid(),time=time.time(),recording_active=False,
                                  processing_started=time.time(),qc_mode=mode,qc_state=summary['qc_state']))
    proof=sync_session(path)
    summary['storage_durability']=proof;meta['storage_durability']=proof
    atomic(path/'capture_summary.json',summary);atomic_yaml(path/'meta.yaml',meta)
    atomic(path/'status.json',dict(state=status,pid=os.getpid(),time=time.time(),recording_active=False,
                                  qc_mode=mode,qc_state=summary['qc_state'],qc_level=summary['qc_level'],errors=summary.get('errors',[]),storage_durability=proof))
    return summary

def _allowed_result_artifact(relative,required):
    parts=PurePosixPath(relative).parts
    if relative in ('qc_report.json','qc_raw_report.json','qc_policy.json','qc_timing.json','qc.html'):
        return True
    if len(parts)!=2:return False
    stream,name=parts
    if stream=='marker':return name=='marker.npz'
    if stream not in required:return False
    if name==stream+'.npz':return True
    if stream=='eeg' and name in ('gate_finalization.json','eeg_summary.json','metadata.json'):return True
    return stream in ('emg_left','emg_right') and name=='timestamp_rebuild.json'


def _result_artifact_source(root,relative):
    source=Path(root)/'artifacts'
    for part in PurePosixPath(relative).parts:
        source=source/part
        try:info=source.lstat()
        except OSError:raise ValueError('QC artifact is missing: '+relative)
        if stat.S_ISLNK(info.st_mode):raise ValueError('QC artifact path contains a symlink: '+relative)
    if not stat.S_ISREG(info.st_mode):raise ValueError('QC artifact is not a regular file: '+relative)
    return source


def _copy_result_artifact(source,destination,expected,session_root):
    destination=Path(destination);session_root=Path(session_root)
    relative=destination.relative_to(session_root);cursor=session_root
    for part in relative.parts[:-1]:
        cursor=cursor/part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or not cursor.is_dir():raise ValueError('QC destination parent is invalid')
        else:cursor.mkdir()
    if destination.parent.is_symlink() or not destination.parent.is_dir():raise ValueError('QC destination parent is invalid')
    if destination.is_symlink():raise ValueError('Symlink in QC result destination')
    if destination.exists():
        if digest(destination)==expected:return
        if destination.relative_to(session_root).as_posix() not in (
                'eeg/metadata.json','eeg/eeg_summary.json','qc_report.json',
                'qc_raw_report.json','qc_policy.json','qc_timing.json','qc.html'):
            raise ValueError('QC artifact would replace existing capture input: '+destination.name)
    temporary=destination.with_name('.'+destination.name+'.qc-import.tmp')
    if temporary.is_symlink():raise ValueError('Symlink at QC artifact staging path')
    if temporary.exists():temporary.unlink()
    descriptor=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        source_fd=os.open(str(source),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        try:
            info=os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode):raise ValueError('QC artifact is not a regular file')
            hasher=hashlib.sha256()
            with os.fdopen(descriptor,'wb') as output:
                while True:
                    block=os.read(source_fd,1024*1024)
                    if not block:break
                    hasher.update(block);output.write(block)
                output.flush();os.fsync(output.fileno())
        finally:os.close(source_fd)
        if hasher.hexdigest()!=expected:raise ValueError('QC artifact changed during import')
        os.replace(str(temporary),str(destination))
        if os.name!='nt':
            directory=os.open(str(destination.parent),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        try:os.unlink(str(temporary))
        except FileNotFoundError:pass


def _apply_input_rows(root,contract):
    """Snapshot local QC inputs for crash recovery of a multi-file apply."""
    from capture_qc_receipt import input_rows
    return input_rows(root,contract,require_marker=False)


def _local_stat(path):
    info=Path(path).lstat()
    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError('Non-regular local QC input: '+str(path))
    return dict(mode=stat.S_IFMT(info.st_mode),device=info.st_dev,inode=info.st_ino,
                size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)


def _journal_inputs_match(root,journal):
    """Allow only this journal's copied outputs and atomic raw-chunk renames."""
    try:
        for rel in journal.get('temporary_paths',[]):
            candidate=Path(root)/rel
            if candidate.is_symlink():return False
            if candidate.exists():
                if not stat.S_ISREG(candidate.lstat().st_mode):return False
                candidate.unlink()
        original={row[0]:row[1] for row in journal['input_rows']}
        current={row[0]:row[1] for row in _apply_input_rows(root,journal['contract'])}
        moved_paths=set()
        for move in journal.get('moves',[]):
            source=move['source'];target=move['target']
            if source not in current and target in current:
                current[source]=current.pop(target);moved_paths.add(source)
            elif source in current and target in current:
                return False
        for rel in journal.get('created_directories',[]):current.pop(rel,None)
        for rel in journal.get('created_artifacts',[]):current.pop(rel,None)
        for rel in journal.get('transformed_artifacts',[]):
            current.pop(rel,None);original.pop(rel,None)
        for rel in journal.get('missing_markers',[]):
            current.pop(rel,None)
            if rel in original and original[rel] is None:current[rel]=None
        if original.keys()!=current.keys():return False
        for rel,expected in original.items():
            actual=current[rel]
            if expected is None:
                if actual is not None:return False
                continue
            if not isinstance(actual,dict):return False
            if expected['mode']==stat.S_IFDIR:
                if any(expected.get(key)!=actual.get(key) for key in ('mode','device','inode')):return False
            elif rel in moved_paths:
                if any(expected.get(key)!=actual.get(key) for key in ('mode','device','inode','size','mtime_ns')):return False
            elif expected!=actual:return False
        return True
    except (OSError,ValueError,TypeError,KeyError,RecursionError):return False


def _move_recorded_chunk(root,move):
    source=Path(root)/move['source'];target=Path(root)/move['target']
    if source.is_symlink() or target.is_symlink():raise ValueError('Symlink in raw chunk archive')
    if source.exists() and not target.exists():
        if digest(source)!=move['sha256']:raise ValueError('Raw chunk changed before archival')
        target.parent.mkdir(exist_ok=True)
        os.replace(str(source),str(target))
        for directory in (target.parent,source.parent):
            if os.name!='nt':
                fd=os.open(str(directory),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
                try:os.fsync(fd)
                finally:os.close(fd)
    elif not source.exists() and target.exists() and digest(target)==move['sha256']:
        return
    else:raise ValueError('Raw chunk archive state conflicts with the apply journal')


def _validate_eeg_finalization(root,source,start,stop):
    proof=_read_json(source,2*1024*1024)
    if proof.get('schema_version')!=1 or proof.get('policy')!='whole_sdk_batches_final_controller_gate_v1':
        raise ValueError('EEG STOP finalization proof schema is invalid')
    start_ns=start.get('start_monotonic_ns');stop_ns=stop.get('stop_monotonic_ns')
    hashes=proof.get('gate_sha256')
    if (type(start_ns) is not int or type(stop_ns) is not int or start_ns<=0 or stop_ns<=start_ns
            or proof.get('start_monotonic_ns')!=start_ns or proof.get('stop_monotonic_ns')!=stop_ns
            or not isinstance(hashes,dict)
            or hashes.get('start')!=digest(Path(root)/'start_gate.json')
            or hashes.get('stop')!=digest(Path(root)/'stop_gate.json')):
        raise ValueError('EEG STOP finalization proof does not match frozen gates')
    values=[proof.get(key) for key in ('input_samples','kept_samples','excluded_samples')]
    if (any(type(value) is not int or value<0 for value in values)
            or values[0]!=values[1]+values[2] or values[1]<=0
            or not isinstance(proof.get('source_chunks'),list) or not proof['source_chunks']):
        raise ValueError('EEG STOP finalization counts or raw chunk list are invalid')
    directory=Path(root)/'eeg';archive=directory/'raw_chunks'
    available={}
    for folder in (directory,archive):
        if not folder.exists():continue
        if folder.is_symlink() or not folder.is_dir():raise ValueError('EEG raw chunk directory is invalid')
        for chunk in folder.glob('chunk_*.npz'):
            if chunk.is_symlink() or not chunk.is_file():raise ValueError('EEG raw chunk is not a regular file')
            if chunk.name in available:raise ValueError('Duplicate EEG raw chunk name')
            available[chunk.name]=chunk
    rows={}
    for item in proof['source_chunks']:
        if not isinstance(item,dict):raise ValueError('Invalid EEG source chunk proof')
        name=item.get('name');size=item.get('bytes');sha=item.get('sha256')
        if (not isinstance(name,str) or Path(name).name!=name or not name.startswith('chunk_')
                or not name.endswith('.npz') or type(size) is not int or size<=0
                or not isinstance(sha,str) or len(sha)!=64 or name in rows):
            raise ValueError('Invalid EEG source chunk identity')
        chunk=available.get(name)
        if chunk is None or chunk.stat().st_size!=size or digest(chunk)!=sha:
            raise ValueError('EEG source chunk does not match STOP proof: '+name)
        rows[name]=dict(name=name,bytes=size,sha256=sha)
    if set(rows)!=set(available):raise ValueError('EEG STOP proof does not cover every raw source chunk')
    return proof


def _validate_eeg_derived_json(source,destination,kind):
    """Allow only the documented STOP-derived EEG statistics to change."""
    if not Path(destination).exists():return False
    original=_read_json(destination,4*1024*1024);derived=_read_json(source,4*1024*1024)
    statistic_keys={'samples','first_sample_index','last_sample_index','invalid_samples','missing_samples',
                    'samples_before_gate_finalization','boundary_excluded_samples'}
    if kind=='metadata':
        for key in set(original)|set(derived):
            if key in ('statistics','gate_finalization'):continue
            if original.get(key)!=derived.get(key):raise ValueError('Host changed non-derived EEG metadata')
        old_stats=original.get('statistics',{});new_stats=derived.get('statistics',{})
        if not isinstance(old_stats,dict) or not isinstance(new_stats,dict):raise ValueError('EEG statistics metadata is invalid')
        for key in set(old_stats)|set(new_stats):
            if key not in statistic_keys and old_stats.get(key)!=new_stats.get(key):
                raise ValueError('Host changed non-derived EEG statistics')
    else:
        for key in set(original)|set(derived):
            if key not in statistic_keys|{'gate_finalization'} and original.get(key)!=derived.get(key):
                raise ValueError('Host changed non-derived EEG summary')
    return digest(destination)!=digest(source)


def apply_result(result_directory,sessions,base):
    """Apply one host report only to its unchanged, frozen Pi capture."""
    from cloud_common import session_lock
    from capture_qc_receipt import verify_input_signature,verify_portable_input_signature,portable_input_signature,write as write_qc_receipt
    from required_device_contract import resolve_selection,qualification_errors
    from orangepi_collect.storage.durability import sync_session
    import ledger
    result_directory=Path(result_directory).resolve();result=_read_json(result_directory/'result.json',16*1024*1024)
    identifier=result.get('session_id','')
    if not identifier or Path(identifier).name!=identifier or identifier in ('.','..'):
        raise ValueError('Invalid session identifier')
    root=Path(sessions).resolve();path=root/identifier
    if path.is_symlink() or not path.is_dir() or path.resolve().parent!=root:raise ValueError('Session not found')
    with session_lock(base,identifier):
        result_sha=digest(result_directory/'result.json');applied=path/'qc_applied.json'
        if applied.exists():
            previous=_read_json(applied,1024*1024)
            if previous.get('result_sha256')!=result_sha:raise ValueError('A different result was already applied')
            return previous
        request=_read_json(path/'qc_pending.json')
        native_sha=digest(path/'native_capture_summary.json')
        if (not isinstance(request.get('input_signature'),str)
                or not isinstance(request.get('local_input_signature'),str)
                or type(request.get('legacy_source')) is not bool
                or not isinstance(result.get('input_signature'),str)
                or not isinstance(result.get('local_input_signature'),str)
                or type(result.get('legacy_source')) is not bool
                or not isinstance(result.get('checked_input_signature'),str)
                or not isinstance(result.get('reference_sha256'),str)):
            raise ValueError('QC_HOST_UPGRADE_REQUIRED: 已保存，主机质检协议需更新后重试；尚未计入完成或上传')
        if (request.get('version')!=1 or request.get('state')!='pending'
                or request.get('session_id')!=identifier or result.get('version')!=1
                or result.get('session_id')!=identifier or result.get('request_id')!=request.get('request_id')
                or request.get('native_sha256')!=native_sha or result.get('native_sha256')!=native_sha
                or result.get('input_signature')!=request.get('input_signature')
                or result.get('local_input_signature')!=request.get('local_input_signature')
                or result.get('legacy_source')!=request.get('legacy_source')
                or result.get('reference_sha256')!=request.get('reference_sha256')
                or len(result.get('checked_input_signature',''))!=64
                or any(ch not in '0123456789abcdef' for ch in result.get('checked_input_signature',''))
                or result.get('level') not in ('INFO','WARN','ERROR')
                or result.get('passed') is not (result.get('level')!='ERROR')):
            raise ValueError('QC result does not match the frozen capture')
        elapsed=result.get('elapsed_seconds');raw_level=result.get('raw_level')
        if (type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0
                or raw_level not in ('INFO','WARN','ERROR')):
            raise ValueError('QC_HOST_UPGRADE_REQUIRED: 主机结果缺少完整运行事实；请升级质检协议后重试')
        worker=_read_json(path/'status.json')
        if worker.get('recording_active') or worker.get('state') not in ('pending_qc','success','qc_error'):
            raise ValueError('Session is still active or saving')
        selection=_read_json(path/'device_selection.json')
        contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
        if contract_errors or contract is None or legacy!=request.get('legacy_source'):
            raise ValueError('Frozen required-device selection changed after QC was requested')
        required=set(contract.get('required_devices',()))
        raw_meta=_read_json(path/'meta.json')
        reference_sha=raw_meta.get('reference_sha256')
        if not reference_sha or reference_sha!=request.get('reference_sha256'):
            raise ValueError('Pending QC reference identity mismatch')
        journal_path=path/'qc_apply_journal.json';journal=None
        if journal_path.exists():
            journal=_read_json(journal_path,8*1024*1024)
            if (journal.get('result_sha256')!=result_sha or journal.get('request_id')!=request.get('request_id')
                    or journal.get('input_signature')!=request.get('input_signature')
                    or journal.get('local_input_signature')!=request.get('local_input_signature')
                    or journal.get('checked_input_signature')!=result.get('checked_input_signature')
                    or journal.get('contract')!=contract):
                raise ValueError('Conflicting or invalid deferred QC recovery journal')
            if not _journal_inputs_match(path,journal):
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 应用中断后原始输入状态无法恢复核验；请检查本段后重试')
        elif (not verify_input_signature(path,request.get('local_input_signature'),contract,require_marker=False)
                or not verify_portable_input_signature(path,request.get('input_signature'),contract,require_marker=False)):
            raise ValueError('The original capture inputs changed after QC was requested')
        artifacts=result.get('artifacts')
        if (not isinstance(artifacts,dict) or 'qc_report.json' not in artifacts
                or len(artifacts)>2048):raise ValueError('QC report or artifact list is missing')
        sources={}
        for rel,expected in artifacts.items():
            if (not isinstance(rel,str) or not rel or '\\' in rel or '\x00' in rel
                    or not isinstance(expected,str) or len(expected)!=64
                    or any(ch not in '0123456789abcdef' for ch in expected)):
                raise ValueError('Invalid QC artifact declaration')
            relative=PurePosixPath(rel)
            if relative.is_absolute() or any(part in ('','.', '..') for part in relative.parts) or not _allowed_result_artifact(rel,set(contract.get('required_devices',()))):
                raise ValueError('QC artifact is not an allowed derived output: '+rel)
            source=_result_artifact_source(result_directory,rel)
            if digest(source)!=expected:raise ValueError('QC artifact checksum mismatch: '+rel)
            sources[rel]=source
        report=_read_json(sources['qc_report.json'],16*1024*1024)
        level=result.get('level')
        if (report.get('level')!=level or report.get('scope','full')!='full'
                or not isinstance(report.get('session_dir'),str)
                or Path(report['session_dir']).name!=identifier):
            raise ValueError('QC_HOST_UPGRADE_REQUIRED: 完整报告与本段身份/范围不匹配；请升级主机质检后重试')
        window=report.get('window')
        if not isinstance(window,dict) or any(type(window.get(key)) not in (int,float) or not math.isfinite(window[key]) for key in ('t0','t1')) or window['t1']<=window['t0']:
            raise ValueError('QC report window is missing or invalid')
        start_path=path/'start_gate.json';stop_path=path/'stop_gate.json'
        start={};stop={}
        if start_path.is_file() and stop_path.is_file():
            start=_read_json(start_path);stop=_read_json(stop_path)
            expected_window=(start.get('start_unix_ns',0)/1e9,stop.get('stop_unix_ns',0)/1e9)
            if (not all(math.isfinite(value) and value>0 for value in expected_window)
                    or abs(window['t0']-expected_window[0])>1e-6
                    or abs(window['t1']-expected_window[1])>1e-6):
                raise ValueError('QC report window does not match the frozen START/STOP gates')
        elif not legacy:
            raise ValueError('QC report window cannot be bound to the frozen START/STOP gates')
        else:
            # Legacy sessions may lack online gate files. Bind to signed native
            # capture times when present; never synthesize gate evidence.
            native=_read_json(path/'native_capture_summary.json')
            start_ns=native.get('start_unix_ns');stop_ns=native.get('stop_unix_ns')
            if type(start_ns) is int and type(stop_ns) is int and start_ns>0 and stop_ns>start_ns:
                expected_window=(start_ns/1e9,stop_ns/1e9)
                if (abs(window['t0']-expected_window[0])>1e-6
                        or abs(window['t1']-expected_window[1])>1e-6):
                    raise ValueError('QC report window does not match legacy capture times')
        eeg_proof=None
        if 'eeg' in required:
            proof_source=sources.get('eeg/gate_finalization.json')
            if proof_source is None:
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 缺少脑电STOP边界证明；请升级主机质检后重试')
            eeg_proof=_validate_eeg_finalization(path,proof_source,
                start if start_path.is_file() else {},stop if stop_path.is_file() else {})
            derived=result.get('derived_counts')
            if not isinstance(derived,dict) or derived.get('eeg_samples')!=eeg_proof['kept_samples']:
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 脑电派生样本数证明缺失或不一致')
        transformed=[]
        for rel,kind in (('eeg/metadata.json','metadata'),('eeg/eeg_summary.json','summary')):
            if rel in sources and (path/rel).exists() and digest(path/rel)!=artifacts[rel]:
                if not _validate_eeg_derived_json(sources[rel],path/rel,kind):
                    raise ValueError('Invalid EEG derived metadata')
                transformed.append(rel)
        if journal is None:
            summary=_read_json(path/'capture_summary.json')
            if summary.get('status')!='pending_qc' or summary.get('errors'):
                raise ValueError('The native capture is not eligible for full deferred QC')
            if (not verify_input_signature(path,request.get('local_input_signature'),contract,require_marker=False)
                    or not verify_portable_input_signature(path,request.get('input_signature'),contract,require_marker=False)):
                raise ValueError('The original capture inputs changed before QC artifacts were applied')
            rows=_apply_input_rows(path,contract)
            if any(Path(rel).name.endswith('.qc-import.tmp') for rel,_ in rows):
                raise ValueError('Unexpected QC staging file in the original capture')
            moves=[];created_directories=set()
            for stream in ('emg_left','emg_right','eeg'):
                directory=path/stream
                if not directory.is_dir():continue
                archive=directory/'raw_chunks'
                if archive.is_symlink() or (archive.exists() and not archive.is_dir()):raise ValueError('Raw chunk archive path is invalid')
                stream_moves=[]
                for chunk in sorted(directory.glob('chunk_*.npz')):
                    target=archive/chunk.name
                    if target.exists() or target.is_symlink():raise ValueError('Raw chunk archive conflict')
                    move=dict(source=chunk.relative_to(path).as_posix(),target=target.relative_to(path).as_posix(),sha256=digest(chunk))
                    moves.append(move);stream_moves.append(move)
                if stream_moves and not archive.exists():
                    created_directories.add(archive.relative_to(path).as_posix())
            created_artifacts=[];temporary_paths=[]
            for rel in sources:
                destination=path/rel
                for parent in destination.parents:
                    if parent==path:break
                    if parent.is_symlink():raise ValueError('Symlink in QC destination path')
                    if not parent.exists():created_directories.add(parent.relative_to(path).as_posix())
                if not destination.exists():created_artifacts.append(rel)
                temporary_paths.append((destination.parent/('.'+destination.name+'.qc-import.tmp')).relative_to(path).as_posix())
            journal=dict(version=1,phase='validated',result_sha256=result_sha,request_id=request.get('request_id'),
                session_id=identifier,input_signature=request.get('input_signature'),
                local_input_signature=request.get('local_input_signature'),
                checked_input_signature=result.get('checked_input_signature'),contract=contract,
                input_rows=rows,native_summary=summary,moves=moves,artifacts=artifacts,
                created_artifacts=created_artifacts,created_directories=sorted(created_directories),
                missing_markers=['marker'] if not (path/'marker').exists() else [],
                transformed_artifacts=transformed,temporary_paths=temporary_paths,created_at=time.time())
            atomic(journal_path,journal)
        journal['phase']='applying';atomic(journal_path,journal)
        for rel,source in sources.items():
            _copy_result_artifact(source,path/rel,artifacts[rel],path)
            if rel not in journal.setdefault('written_artifacts',[]):
                journal['written_artifacts'].append(rel);atomic(journal_path,journal)
        for move in journal.get('moves',[]):
            _move_recorded_chunk(path,move)
            if move['target'] not in journal.setdefault('moved_chunks',[]):
                journal['moved_chunks'].append(move['target']);atomic(journal_path,journal)
        checked=portable_input_signature(path,contract,require_marker=True)
        if checked!=result.get('checked_input_signature'):
            raise ValueError('QC_HOST_UPGRADE_REQUIRED: 应用数据与主机完成质检时不一致；本段未计入完成')
        summary=dict(journal['native_summary'])
        if eeg_proof is not None:
            details=summary.get('eeg')
            if not isinstance(details,dict):raise ValueError('EEG capture summary is missing')
            summary_details=dict(details)
            summary_details.update(samples=eeg_proof['kept_samples'],
                samples_before_gate_finalization=eeg_proof['input_samples'],
                boundary_excluded_samples=eeg_proof['excluded_samples'],
                gate_finalization='eeg/gate_finalization.json')
            if 'eeg/eeg_summary.json' in sources:
                eeg_summary=_read_json(path/'eeg/eeg_summary.json',4*1024*1024)
                for key in ('first_sample_index','last_sample_index','invalid_samples','missing_samples'):
                    if type(eeg_summary.get(key)) is int:summary_details[key]=eeg_summary[key]
            summary['eeg']=summary_details
        candidate=dict(summary,status='success',qc_state='completed',qc_level=level,
                       qc_raw_level=result.get('raw_level'),qc_elapsed_seconds=result.get('elapsed_seconds'),qc_executor='linux_host')
        from required_device_contract import data_warnings
        legacy_warnings=data_warnings(summary,legacy=legacy)
        if legacy_warnings:
            candidate['warnings']=list(dict.fromkeys(list(candidate.get('warnings',[]))+[
                '历史记录缺少新版必需设备START在线证明；原始数据已由本轮完整质检核验']))
        errors=qualification_errors(path,candidate,contract,report,legacy=legacy)
        outcome='success' if level in ('INFO','WARN') and not errors else 'qc_error'
        proof=sync_session(path)
        summary.update(status=outcome,capture_status='success',qc_state='completed',qc_level=level,
                       qc_raw_level=result.get('raw_level'),qc_elapsed_seconds=result.get('elapsed_seconds'),
                       qc_executor='linux_host',storage_durability=proof)
        if legacy_warnings:
            summary['warnings']=list(dict.fromkeys(list(summary.get('warnings',[]))+[
                '历史记录缺少新版必需设备START在线证明；原始数据已由本轮完整质检核验']))
        if outcome!='success':
            summary['errors']=list(dict.fromkeys(list(summary.get('errors',[]))+(['主机完整质检发现 ERROR。'] if level=='ERROR' else [])+errors))
        meta=yaml.safe_load((path/'meta.yaml').read_text(encoding='utf-8')) or {}
        if not isinstance(meta,dict):raise ValueError('Local session metadata is invalid')
        meta.update(session_dir=str(path),status=outcome,qc_state='completed',qc_level=level,
                    qc_raw_level=result.get('raw_level'),qc_mode='deferred',qc_executor='linux_host',
                    storage_durability=proof,retention_state='kept' if outcome=='success' else 'kept_failed',
                    retained_at_unix=time.time(),reference_state='collected' if outcome=='success' else 'uncollected',
                    upload_state='retained_for_upload' if outcome=='success' else 'excluded')
        atomic(path/'capture_summary.json',summary);ledger.atomic_yaml(path/'meta.yaml',meta)
        atomic(path/'status.json',dict(state=outcome,time=time.time(),recording_active=False,
                                      qc_state='completed',qc_level=level,errors=summary.get('errors',[]),storage_durability=proof))
        if outcome=='success':
            try:write_qc_receipt(path)
            except (OSError,ValueError,TypeError,KeyError,RecursionError) as error:
                outcome='qc_error';summary['status']=outcome;summary['qc_state']='failed'
                summary['errors']=list(dict.fromkeys(list(summary.get('errors',[]))+['完整QC回执签发失败：'+str(error)]))
                try:(path/'capture_qc_receipt.json').unlink()
                except FileNotFoundError:pass
                meta.update(status=outcome,qc_state='failed',retention_state='kept_failed',reference_state='uncollected',upload_state='excluded')
                atomic(path/'capture_summary.json',summary);ledger.atomic_yaml(path/'meta.yaml',meta)
                atomic(path/'status.json',dict(state=outcome,time=time.time(),recording_active=False,qc_state='failed',qc_level=level,errors=summary['errors']))
        reference=meta.get('reference_image') or (meta.get('pi') or {}).get('reference_image')
        if reference:
            image=ledger.PROJECT/reference
            result_mark=ledger.mark(image,path,'success' if outcome=='success' else 'qc_error')
            if outcome=='success' and result_mark is False:
                outcome='qc_error';summary['status']=outcome;summary['qc_state']='failed'
                summary['errors']=list(dict.fromkeys(list(summary.get('errors',[]))+['账本完整资格复核未通过。']))
                try:(path/'capture_qc_receipt.json').unlink()
                except FileNotFoundError:pass
                meta.update(status=outcome,qc_state='failed',retention_state='kept_failed',reference_state='uncollected',upload_state='excluded')
                atomic(path/'capture_summary.json',summary);ledger.atomic_yaml(path/'meta.yaml',meta)
                atomic(path/'status.json',dict(state=outcome,time=time.time(),recording_active=False,qc_state='failed',qc_level=level,errors=summary['errors']))
                ledger.mark(image,path,'qc_error')
            elif outcome!='success':ledger.retain_error(image,path)
        elif outcome=='success':
            raise ValueError('Qualified capture has no bound reference image')
        receipt=dict(version=1,session_id=identifier,result_sha256=result_sha,level=level,
                     passed=outcome=='success',outcome=outcome,applied_at=time.time())
        atomic(applied,receipt)
        atomic(Path(base)/'runtime/qc_results_revision.json',dict(at=time.time(),session_id=identifier,result_sha256=result_sha))
        return receipt
