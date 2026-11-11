"""Original per-scene used.yaml contract, atomic updates and preserved failed takes."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import fcntl,hashlib,json,os,re,stat,time
from pathlib import Path
import yaml
from project_paths import BASE, PROJECT, SRC, devices
LIBRARY=PROJECT/'configs/environments'
# Use the safe C parser when installed; keep the same safe constructors and
# a pure-Python fallback. Do not cache verdicts: deleted/changed takes must be
# reflected on every read. Large per-take metadata otherwise stalls mode entry.
_SAFE_YAML_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)

def _load_yaml(text):
    return yaml.load(text, Loader=_SAFE_YAML_LOADER)

BAD={'failed','error','qc_error','retry','interrupted','cancelled','recording','postprocessing','deleted','archived'}

def atomic_yaml(path,value):
    temp=path.with_suffix('.yaml.tmp')
    with temp.open('w',encoding='utf-8') as f:
        yaml.safe_dump(value,f,allow_unicode=True,sort_keys=False);f.flush();os.fsync(f.fileno())
    temp.replace(path)
    fd=os.open(str(path.parent),os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def read(path):
    if not path.exists():return {'used':{}}
    # Ledger keys are Chinese reference-image paths; atomic_yaml writes UTF-8,
    # so read it back as UTF-8 rather than following the ambient locale.
    value=_load_yaml(path.read_text(encoding='utf-8')) or {}
    if not isinstance(value,dict) or not isinstance(value.get('used',{}),dict):raise ValueError('图片台账格式错误：'+str(path))
    return value

def _read_json(path,limit=32*1024*1024):
    path=Path(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('invalid ledger evidence file')
    def reject(value):raise ValueError('non-finite JSON value')
    value=json.loads(path.read_text(encoding='utf-8'),parse_constant=reject)
    if not isinstance(value,dict):raise ValueError('ledger evidence must be an object')
    return value

def _session_matches_image(session,image):
    if image is None:return True
    try:
        image=Path(image).resolve();image.relative_to(LIBRARY.resolve())
        relative=image.relative_to(PROJECT.resolve()).as_posix()
        library_relative=image.relative_to(LIBRARY.resolve()).as_posix()
        meta=_load_yaml((Path(session)/'meta.yaml').read_text(encoding='utf-8')) or {}
        if not isinstance(meta,dict):return False
        values=[meta.get('reference_image'),meta.get('environment'),
                (meta.get('pi') or {}).get('reference_image') if isinstance(meta.get('pi'),dict) else None]
        selection=meta.get('reference_selection')
        asset=selection.get('asset') if isinstance(selection,dict) else None
        if isinstance(asset,dict):values.append(asset.get('relative_path'))
        accepted={relative,'configs/environments/'+library_relative,library_relative}
        return any(isinstance(value,str) and
                   (value[len('configs/environments/'):] if value.startswith('configs/environments/') else value) in accepted
                   for value in values)
    except (OSError,ValueError,TypeError,AttributeError,yaml.YAMLError):return False

def _qualified_session(session,image=None):
    session=Path(session)
    try:
        if session.is_symlink() or not session.is_dir() or (session/'recording_deleted.json').exists():return False
        summary=_read_json(session/'capture_summary.json')
        meta=_load_yaml((session/'meta.yaml').read_text(encoding='utf-8')) or {}
        if (summary.get('status')!='success' or summary.get('errors')
                or not isinstance(meta,dict) or meta.get('status')!='success'):
            return False
        if image is not None and not _session_matches_image(session,image):return False
        from capture_qc_receipt import verify
        return not verify(session)
    except (OSError,ValueError,TypeError,KeyError,RecursionError,ImportError,yaml.YAMLError):return False

def _uploaded_completion(entry,image):
    """Accept a cleaned session only with its bound successful upload receipt."""
    if image is None or not isinstance(entry,dict):return False
    session=Path(str(entry.get('session','')))
    if not session.name:return False
    try:
        relative=Path(image).resolve().relative_to(PROJECT.resolve()).as_posix()
        root=BASE/'runtime/cloud_uploads';queue=_read_json(root/'queue.json')
        jobs=queue.get('jobs')
        if not isinstance(jobs,list):return False
        for job in jobs:
            if not isinstance(job,dict) or job.get('session_id')!=session.name or job.get('local_deleted') is not True:
                continue
            recording=job.get('recording')
            if (not isinstance(recording,dict) or recording.get('id')!=session.name
                    or recording.get('qualified') is not True or recording.get('reference_path')!=relative):continue
            if job.get('state')!='success' or Path(str(job.get('source',''))).name!=session.name:continue
            job_id=job.get('id')
            if not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',job_id):continue
            directory=root/'jobs'/job_id
            if directory.is_symlink() or not directory.is_dir():continue
            receipt=_read_json(directory/'receipt.json',2*1024*1024)
            manifest=_read_json(directory/'manifest.json',16*1024*1024)
            if (receipt.get('status')=='success' and receipt.get('platform_completion_acknowledged') is True
                    and receipt.get('session_id')==session.name and receipt.get('job_id')==job_id
                    and isinstance(receipt.get('manifest_digest'),str)
                    and receipt.get('manifest_digest')==manifest.get('digest')):
                return True
        return False
    except (OSError,ValueError,TypeError,KeyError,RecursionError):return False

def _server_handoff_completion(entry,image):
    """Verify a Pi handoff tombstone from its signed custody and QC receipts."""
    if (not isinstance(entry,dict) or entry.get('server_qc') is not True
            or entry.get('data_location')!='host' or entry.get('pi_original_deleted') is not True
            or entry.get('retained') is not True):return False
    try:
        session_value=entry.get('session')
        if not isinstance(session_value,str) or not session_value:return False
        source=Path(session_value)
        sid=source.name
        if not sid or source.exists() or source.is_symlink():return False
        # Manual handoff state and the shared HMAC key live beside the common
        # collector code, even when the active collection profile has its own
        # BASE/PROJECT tree.
        shared_base=Path(project_paths.CODE_BASE).resolve()
        state_root=shared_base/'runtime/server_qc'
        if state_root.is_symlink() or not state_root.is_dir():return False
        record_path=state_root/'handoffs'/(sid+'.json')
        result_path=state_root/'results'/(sid+'.json')
        if (record_path.parent.is_symlink() or result_path.parent.is_symlink()
                or record_path.resolve().parent!=(state_root/'handoffs').resolve()
                or result_path.resolve().parent!=(state_root/'results').resolve()):return False
        record=_read_json(record_path,16*1024*1024);applied=_read_json(result_path,2*1024*1024)
        if (record.get('purpose')!='raw_handoff_v1' or record.get('state')!='deleted'
                or record.get('session_id')!=sid or record.get('source_path')!=session_value
                or record.get('receipt_sha256')!=entry.get('handoff_receipt_sha256')
                or applied.get('purpose')!='post_handoff_qc_v1' or applied.get('session_id')!=sid
                or applied.get('passed') is not True or applied.get('retake_required') is not False
                or applied.get('level') not in ('INFO','WARN')
                or applied.get('qc_protocol_version')!=2
                or applied.get('qc_qualification_protocol')!='required-device-contract-v1'
                or applied.get('request_id')!=record.get('qc_request_id')
                or applied.get('native_sha256')!=record.get('native_sha256')
                or applied.get('result_sha256')!=entry.get('result_sha256')
                or entry.get('qc_level')!=applied.get('level')
                or applied.get('handoff_receipt_sha256')!=entry.get('handoff_receipt_sha256')
                or applied.get('qualification_receipt_sha256')!=entry.get('qualification_receipt_sha256')
                or applied.get('reference_sha256')!=entry.get('reference_sha256')
                or applied.get('server_path')!=entry.get('server_path')):return False

        from raw_handoff import (RAW_PURPOSE,RESULT_PURPOSE,validate_raw_receipt,
                                 verify_signature,sha_value)
        key_path=shared_base/'private/server_qc_hmac'
        key_info=key_path.lstat()
        if not stat.S_ISREG(key_info.st_mode):return False
        key=key_path.read_bytes()
        raw=record.get('receipt')
        if (not isinstance(raw,dict) or record.get('receipt_signature')!=raw.get('signature')
                or sha_value(raw)!=record.get('receipt_sha256')):return False
        manifest=raw.get('manifest')
        if not isinstance(manifest,dict) or manifest.get('root')!=str(source.parent):return False
        validate_raw_receipt(raw,key,record.get('device_id'),record.get('server_id'),manifest['root'])
        if (raw.get('purpose')!=RAW_PURPOSE or raw.get('session_id')!=sid
                or raw.get('qc_protocol_version')!=2
                or raw.get('qc_qualification_protocol')!='required-device-contract-v1'
                or raw.get('input_signature')!=record.get('source_input_signature')
                or raw.get('local_input_signature')!=record.get('source_local_input_signature')
                or raw.get('reference_sha256')!=record.get('source_meta',{}).get('reference_sha256')
                or record.get('qc_request_id')!=raw.get('qc_request_id')
                or record.get('native_sha256')!=manifest.get('native_sha256')):return False

        signed_result=applied.get('handoff_result_receipt')
        envelope=verify_signature(signed_result,key,RESULT_PURPOSE)
        qualification_sha=applied.get('qualification_receipt_sha256')
        checked=applied.get('checked_input_signature')
        signature=applied.get('input_signature');local=applied.get('local_input_signature')
        reference_sha=applied.get('reference_sha256')
        result_sha=applied.get('result_sha256')
        if (envelope.get('device_id')!=record.get('device_id')
                or envelope.get('server_id')!=record.get('server_id')
                or envelope.get('session_id')!=sid
                or envelope.get('handoff_receipt_sha256')!=record.get('receipt_sha256')
                or envelope.get('result_sha256')!=result_sha
                or envelope.get('passed') is not True
                or envelope.get('qc_protocol_version')!=2
                or envelope.get('qc_qualification_protocol')!='required-device-contract-v1'
                or envelope.get('input_signature')!=raw.get('input_signature')
                or envelope.get('local_input_signature')!=raw.get('local_input_signature')
                or envelope.get('checked_input_signature')!=checked
                or envelope.get('reference_sha256')!=raw.get('reference_sha256')
                or envelope.get('qualification_receipt_sha256')!=qualification_sha
                or not isinstance(envelope.get('server_path'),str)
                or not Path(envelope['server_path']).is_absolute()
                or envelope.get('reference_sha256')!=record.get('source_meta',{}).get('reference_sha256')
                or not isinstance(qualification_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',qualification_sha)
                or not isinstance(result_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',result_sha)
                or not isinstance(signature,str) or len(signature)!=64
                or signature!=raw.get('input_signature')
                or local!=raw.get('local_input_signature')
                or reference_sha!=raw.get('reference_sha256')
                or not isinstance(checked,str) or not re.fullmatch(r'[0-9a-f]{64}',checked)
                or envelope.get('server_path')!=applied.get('server_path')):return False

        meta=record.get('source_meta')
        if not isinstance(meta,dict):return False
        selection=record.get('frozen_selection')
        if not isinstance(selection,dict):return False
        from required_device_contract import resolve_selection
        contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
        current_mode=getattr(project_paths,'COLLECTION_MODE',None)
        if (contract_errors or contract is None or legacy
                or contract.get('version')!='required-devices-v1'
                or not isinstance(current_mode,str)
                or selection.get('collection_mode')!=current_mode
                or contract.get('mode')!=current_mode
                or meta.get('collection_mode')!=current_mode
                or raw.get('required_device_contract_version')!=contract.get('version')):return False
        reference=meta.get('reference_image')
        if not isinstance(reference,str) or not reference:return False
        frozen_project=Path(record.get('reference_project','')).resolve()
        current_project=Path(project_paths.PROJECT).resolve()
        if frozen_project!=current_project:return False
        candidate_image=frozen_project/reference
        if candidate_image.is_symlink():return False
        frozen_image=candidate_image.resolve()
        frozen_image.relative_to((frozen_project/'configs/environments').resolve())
        if image is not None and Path(image).resolve()!=frozen_image:return False
        if (not frozen_image.is_file() or frozen_image.is_symlink()
                or hashlib.sha256(frozen_image.read_bytes()).hexdigest()!=reference_sha
                or meta.get('reference_sha256')!=reference_sha):return False
        return True
    except (OSError,ValueError,TypeError,KeyError,RecursionError,ImportError):return False

def reserved_used(entry,image=None):
    """A durable pending-QC take reserves a task without completing it."""
    if not isinstance(entry,dict) or entry.get('status')!='pending_qc':return False
    try:
        value=entry.get('session')
        if not isinstance(value,str) or not value:return False
        session=Path(value)
        if session.is_symlink() or not session.is_dir() or (session/'recording_deleted.json').exists():return False
        summary=_read_json(session/'capture_summary.json')
        meta=_load_yaml((session/'meta.yaml').read_text(encoding='utf-8')) or {}
        proof=summary.get('storage_durability')
        if (summary.get('status')!='pending_qc' or not isinstance(proof,dict) or proof.get('completed') is not True
                or not isinstance(meta,dict) or meta.get('status')!='pending_qc'):
            return False
        return image is None or _session_matches_image(session,image)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,yaml.YAMLError):return False

def valid_used(entry,image=None):
    """Only a currently verified full-QC receipt or bound upload tombstone completes a task."""
    if not isinstance(entry,dict) or entry.get('status')!='success':return False
    try:
        value=entry.get('session')
        if not isinstance(value,str) or not value:return False
        session=Path(value)
    except (TypeError,ValueError):return False
    if _qualified_session(session,image):return True
    try:
        if not session.exists() and not session.is_symlink():
            return _uploaded_completion(entry,image) or _server_handoff_completion(entry,image)
    except (OSError,ValueError,TypeError):return False
    return False

def images():
    return sorted(p for p in LIBRARY.rglob('*.png') if re.search(r'(?:_\d+|\d+_combo\d+_r\d+)$',p.stem))

def unused():
    result=[];cache={}
    for image in images():
        ledger=image.parent/'used.yaml'
        if ledger not in cache:cache[ledger]=read(ledger).get('used',{})
        entry=cache[ledger].get(image.name)
        if not valid_used(entry,image) and not reserved_used(entry,image):result.append(image)
    return result

def mark(image,session,status):
    image=Path(image).resolve();image.relative_to(LIBRARY.resolve());session=Path(session)
    if status not in BAD|{'success','pending_qc'}:return False
    if status=='success' and not _qualified_session(session,image):return False
    if status=='pending_qc' and not reserved_used({'session':str(session),'status':'pending_qc'},image):return False
    target=image.parent/'used.yaml'
    lockroot=BASE/'runtime';lockroot.mkdir(exist_ok=True)
    with (lockroot/'ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        value=read(target)
        if target.exists():
            backup=BASE/'runtime/ledger_backups';backup.mkdir(parents=True,exist_ok=True)
            identity=hashlib.sha256(str(target).encode()).hexdigest()[:16]
            (backup/(identity+'_'+str(time.time_ns())+'.yaml')).write_bytes(target.read_bytes())
        used=value.setdefault('used',{})
        summary={}
        try:summary=json.loads((Path(session)/'capture_summary.json').read_text())
        except (OSError,ValueError):pass
        if not isinstance(summary,dict):summary={}
        has_data=status in ('success','pending_qc') or any(isinstance(v,dict) and any(isinstance(v.get(k),(int,float)) and v.get(k,0)>0 for k in ('frames','emg_frames','samples_saved','samples_written')) for v in summary.values())
        if has_data:
            attempts=value.setdefault('attempts',{}).setdefault(image.name,[])
            quality=('PENDING' if status=='pending_qc' else summary.get('qc_level')) or ('ERROR' if status in BAD else 'INFO')
            previous=next((v for v in attempts if v.get('session')==str(session)),None)
            item=dict(session=str(session),at=time.time(),status=status,qc_level=quality,has_data=True,deleted=False,retained=status in ('success','pending_qc'))
            if previous is None:attempts.append(item)
            else:previous.update(item)
        if status in ('success','pending_qc'):
            existing=used.get(image.name)
            if not (status=='pending_qc' and isinstance(existing,dict) and existing.get('status')!='pending_qc' and valid_used(existing,image)):
                used[image.name]={'session':str(session),'at':time.strftime('%Y-%m-%d %H:%M:%S'),'status':status,'qc_level':quality}
            value.get('failed',{}).pop(image.name,None)
        else:
            if image.name in used and (not valid_used(used[image.name],image) or used[image.name].get('session')==str(session)):
                used.pop(image.name,None)
            value.setdefault('failed',{})[image.name]={'session':str(session),'at':time.strftime('%Y-%m-%d %H:%M:%S'),'status':status}
        atomic_yaml(target,value)
        return True

def migrate_legacy():
    old=BASE/'runtime/used.json';done=BASE/'runtime/ledger_migration_v4.json'
    if done.exists():return
    migrated=[]
    if old.exists():
        for rel,entry in json.loads(old.read_text(encoding='utf-8')).items():
            image=PROJECT/rel;session=Path(entry['session']);meta=session/'meta.yaml'
            if image.exists() and meta.exists() and (_load_yaml(meta.read_text(encoding='utf-8')) or {}).get('status')=='success':
                existing=read(image.parent/'used.yaml').get('used',{}).get(image.name)
                if existing is None or not valid_used(existing,image):
                    if not mark(image,session,'success'):continue
                migrated.append(rel)
    done.write_text(json.dumps({'migrated':migrated,'at':time.time()},ensure_ascii=False,indent=2),encoding='utf-8')


def release(image,session):
    """Release only the take that currently owns the reference completion."""
    image=Path(image).resolve();image.relative_to(LIBRARY.resolve());target=image.parent/'used.yaml'
    lockroot=BASE/'runtime';lockroot.mkdir(exist_ok=True)
    with (lockroot/'ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);value=read(target);used=value.setdefault('used',{});entry=used.get(image.name)
        attempts=value.get('attempts',{}).get(image.name,[])
        for attempt in attempts:
            if attempt.get('session')==str(session):attempt.update(deleted=True,retained=False,deleted_at=time.time())
        if not isinstance(entry,dict) or str(entry.get('session',''))!=str(session):
            if attempts:atomic_yaml(target,value)
            return False
        backup=lockroot/'ledger_backups';backup.mkdir(exist_ok=True)
        identity=hashlib.sha256(str(target).encode()).hexdigest()[:16]
        if target.exists():(backup/(identity+'_'+str(time.time_ns())+'_released.yaml')).write_bytes(target.read_bytes())
        used.pop(image.name,None);value.get('failed',{}).pop(image.name,None);atomic_yaml(target,value)
        return True


def retain_error(image,session):
    image=Path(image).resolve();image.relative_to(LIBRARY.resolve());target=image.parent/'used.yaml'
    with (BASE/'runtime/ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);value=read(target)
        for attempt in value.get('attempts',{}).get(image.name,[]):
            if attempt.get('session')==str(session):attempt.update(retained=True,retained_at=time.time())
        atomic_yaml(target,value)

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['list','migrate','failed']);p.add_argument('--session',type=Path);a=p.parse_args()
    if a.action=='list':
        for image in unused():print(image.relative_to(LIBRARY))
    elif a.action=='migrate':migrate_legacy()
    else:
        if not a.session:p.error('--session is required')
        session=a.session.resolve()
        import storage_paths
        allowed=[PROJECT.resolve()]
        if PROJECT.resolve() not in session.parents:allowed.append(storage_paths.session_root().resolve())
        if not any(session==root or root in session.parents for root in allowed):raise ValueError('会话不在项目或配置的 SD 卡数据目录内')
        meta=read(session/'meta.yaml');rel=meta.get('environment')
        if not rel:raise ValueError('会话缺少 environment，不能定位图片')
        meta['status']='failed';atomic_yaml(session/'meta.yaml',meta)
        mark(LIBRARY/rel,session,'failed');print('已标记 failed，保留数据并重新进入未采集池。')
