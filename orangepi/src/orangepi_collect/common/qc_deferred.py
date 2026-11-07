"""Durable pending-QC state. Saving a capture does not imply a QC pass."""
import hashlib
import json
import os
from pathlib import Path
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

def save_pending(path,summary,base,project,emg_report=None,eeg_report=None):
    from orangepi_collect.storage.durability import sync_session
    from orangepi_collect.storage.ledger import atomic_yaml
    path=Path(path)
    if summary.get('status')!='success' or summary.get('errors'):
        raise ValueError('Only a successfully closed native capture may await QC')
    stop=json.loads((path/'stop_gate.json').read_text())
    if stop.get('recording_active') is not False or not (path/'start_gate.json').is_file():
        raise ValueError('Capture START/STOP confirmation is missing')
    raw=json.loads((path/'meta.json').read_text(encoding='utf-8'))
    pending=dict(version=1,request_id=uuid.uuid4().hex,session_id=path.name,state='pending',
                 native_sha256=digest(path/'native_capture_summary.json'),
                 reference_sha256=raw.get('reference_sha256'),created_at=time.time())
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

def apply_result(result_directory,sessions,base):
    """Idempotent import; only this session's reservation is released on ERROR."""
    from cloud_common import session_lock
    import ledger
    result_directory=Path(result_directory).resolve();result=json.loads((result_directory/'result.json').read_text())
    identifier=result.get('session_id','')
    if not identifier or Path(identifier).name!=identifier or identifier in ('.','..'):
        raise ValueError('Invalid session identifier')
    root=Path(sessions).resolve();path=root/identifier
    if path.is_symlink() or not path.is_dir() or path.resolve().parent!=root:raise ValueError('Session not found')
    with session_lock(base,identifier):
        request=json.loads((path/'qc_pending.json').read_text())
        if (result.get('version')!=1 or result.get('request_id')!=request['request_id']
                or result.get('native_sha256')!=digest(path/'native_capture_summary.json')
                or result.get('level') not in ('INFO','WARN','ERROR')):
            raise ValueError('QC result does not match the frozen capture')
        result_sha=digest(result_directory/'result.json');applied=path/'qc_applied.json'
        if applied.exists():
            previous=json.loads(applied.read_text())
            if previous['result_sha256']!=result_sha:raise ValueError('A different result was already applied')
            return previous
        worker=json.loads((path/'status.json').read_text())
        if worker.get('recording_active') or worker.get('state') not in ('pending_qc','success','qc_error'):
            raise ValueError('Session is still active or saving')
        artifacts=result.get('artifacts',{})
        if 'qc_report.json' not in artifacts:raise ValueError('QC report is missing')
        for rel,expected in artifacts.items():
            relative=Path(rel)
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Invalid result artifact')
            source=result_directory/'artifacts'/relative
            if source.is_symlink() or not source.is_file() or digest(source)!=expected:raise ValueError('Result artifact checksum mismatch: '+rel)
        report=json.loads((result_directory/'artifacts/qc_report.json').read_text())
        if report.get('level')!=result['level']:raise ValueError('QC report level mismatch')
        import shutil
        for rel in artifacts:
            destination=path/rel;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.is_symlink():raise ValueError('Symlink in result destination')
            temporary=destination.with_name(destination.name+'.qc-import')
            shutil.copyfile(result_directory/'artifacts'/rel,temporary)
            with temporary.open('rb') as f:os.fsync(f.fileno())
            os.replace(temporary,destination)
        # Retain the original raw chunks, but place them below the stream so
        # downstream checkers see one canonical top-level NPZ per EMG stream.
        for stream in ('emg_left','emg_right'):
            directory=path/stream
            if not (directory/(stream+'.npz')).exists():continue
            for chunk in directory.glob('chunk_*.npz'):
                archive=directory/'raw_chunks';archive.mkdir(exist_ok=True);target=archive/chunk.name
                if target.exists():
                    if digest(target)!=digest(chunk):raise ValueError('Raw chunk archive conflict')
                    raise ValueError('Duplicate top-level raw chunk; resolve before importing')
                chunk.rename(target)
        summary=json.loads((path/'capture_summary.json').read_text())
        outcome='qc_error' if result['level']=='ERROR' else 'success'
        summary.update(status=outcome,qc_state='completed',qc_level=result['level'],
                       qc_raw_level=result.get('raw_level'),qc_elapsed_seconds=result['elapsed_seconds'],qc_executor='linux_host')
        meta=yaml.safe_load((path/'meta.yaml').read_text(encoding='utf-8')) or {}
        meta.update(result.get('metadata',{}))
        meta.update(session_dir=str(path),status=outcome,qc_state='completed',qc_level=result['level'],
                    retention_state='kept_failed' if outcome=='qc_error' else 'kept',retained_at_unix=time.time(),
                    reference_state='uncollected' if outcome=='qc_error' else 'collected',
                    upload_state='retained_unsuccessful_for_upload' if outcome=='qc_error' else 'retained_for_upload')
        atomic(path/'capture_summary.json',summary);ledger.atomic_yaml(path/'meta.yaml',meta)
        reference=meta.get('reference_image') or (meta.get('pi') or {}).get('reference_image')
        if reference:
            image=ledger.PROJECT/reference;ledger.mark(image,path,'error' if outcome=='qc_error' else 'success')
            if outcome=='qc_error':ledger.retain_error(image,path)
        atomic(path/'status.json',dict(state=outcome,time=time.time(),recording_active=False,qc_state='completed',qc_level=result['level']))
        receipt=dict(version=1,session_id=identifier,result_sha256=result_sha,level=result['level'],
                     passed=result['level']!='ERROR',applied_at=time.time())
        atomic(applied,receipt)
        atomic(Path(base)/'runtime/qc_results_revision.json',dict(at=time.time(),session_id=identifier,result_sha256=result_sha))
        return receipt
