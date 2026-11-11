#!/usr/bin/env python3
"""Pi-side finalized-session protocol. No recorder restarts, no broad deletions."""
import contextlib,datetime,fcntl,hashlib,hmac,json,os,re,shutil,stat,sys,time,uuid
from pathlib import Path
B=Path(os.environ.get('HEADRING_PI_BASE','/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi'))
sys.path.insert(0,str(B/'src/orangepi_collect/common'))
import project_paths
import yaml
import ledger,storage_paths
from cloud_common import atomic,session_lock,source_manifest,same_manifest,sync_dir
from raw_handoff import RAW_PURPOSE,RESULT_PURPOSE,validate_raw_receipt,verify_signature,sha_value,same_file_bytes

ID_RE=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
TERMINAL=('pending_qc','success','qc_error')
STATE=B/'runtime/server_qc';STATE.mkdir(parents=True,exist_ok=True)
RESULTS=Path('/home/orangepi/maintenance/server-qc/results')

def load(path,default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return {} if default is None else default

def identity():return Path('/opt/egorecord/DEVICE_ID').read_text().strip()

def roots():
    root=storage_paths.session_root().resolve();volume=storage_paths.volume_root().resolve()
    root.relative_to(volume)
    if root==volume or not str(volume).startswith('/media/orangepi/sdcard/'):
        raise ValueError('Unexpected session volume')
    return root,volume

def current(light=True):
    import urllib.request
    endpoint='/api/display-state' if light else '/api/status?display=server-qc-agent&signals=&cameras=0&review=0'
    request=urllib.request.Request('http://127.0.0.1:8765'+endpoint,headers={'X-Access-Token':(B/'runtime/dashboard_token').read_text().strip()})
    with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)

def idle():
    value=current();worker=value.get('worker') or {}
    if value.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or value.get('session') or worker.get('recording_active'):
        raise RuntimeError('PI_BUSY: '+json.dumps({'state':value.get('state'),'session':value.get('session'),'worker_state':worker.get('state')},ensure_ascii=False))
    return value

def idle_progress():
    last=[time.monotonic()]
    def check(*args):
        if time.monotonic()-last[0]>=2:idle();last[0]=time.monotonic()
    return check

def session(identifier):
    if not isinstance(identifier,str) or not ID_RE.fullmatch(identifier) or identifier in ('.','..'):
        raise ValueError('Invalid session identifier')
    root,_=roots();path=root/identifier
    if path.is_symlink() or not path.is_dir() or path.resolve().parent!=root:
        raise ValueError('Session directory missing or changed')
    return path

def finalized(path):
    idle()
    stop=load(path/'stop_gate.json');status=load(path/'status.json')
    if stop.get('recording_active') is not False or status.get('recording_active') is True or status.get('state') not in TERMINAL:
        raise RuntimeError('Session is not a closed terminal capture')
    if not (path/'start_gate.json').is_file() or not (path/'native_capture_summary.json').is_file():
        raise RuntimeError('Frozen capture START/STOP/native proof missing')
    cfg=storage_paths.config()
    import subprocess
    actual=subprocess.check_output(['findmnt','-n','-o','UUID','--target','/media/orangepi/sdcard'],text=True).strip()
    if cfg.get('expected_uuid') and actual!=cfg['expected_uuid']:
        raise RuntimeError('SD card UUID changed')
    return status

def manifest(identifier):
    path=session(identifier)
    with session_lock(B,identifier):
        finalized(path);last=[time.monotonic()]
        def progress(*args):
            if time.monotonic()-last[0]>2:idle();last[0]=time.monotonic()
        cache=load(STATE/'manifests'/(identifier+'.json'))
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        known={(x['inode'],x['bytes'],x['mtime_ns']):x.get('sha256') for x in cache.get('files',[])} if cache.get('boot_id')==boot else {}
        value=source_manifest(path,hashes=False,progress=progress)
        for row in value['files']:
            key=(row['inode'],row['bytes'],row['mtime_ns']);digest=known.get(key)
            if not digest:
                p=path/row['path'];h=hashlib.sha256()
                with p.open('rb') as f:
                    for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk);progress()
                info=p.stat()
                if (info.st_ino,info.st_size,info.st_mtime_ns)!=key:raise RuntimeError('File changed during hashing')
                digest=h.hexdigest()
            row['sha256']=digest
        value['digest']=hashlib.sha256(json.dumps(value['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        finalized(path)
        info=path.stat();value.update(device_id=identity(),session_id=identifier,root=str(path.parent),boot_id=boot,
            source_dev=info.st_dev,source_inode=info.st_ino,
            native_sha256=hashlib.sha256((path/'native_capture_summary.json').read_bytes()).hexdigest())
        atomic(STATE/'manifests'/(identifier+'.json'),value)
        return value

def scan(request):
    status=current(light=False);root,volume=roots();items=[];cutoff=request['cutoff'];day=request['day']
    for path in sorted(root.iterdir()) if root.exists() else []:
        if path.is_symlink() or not path.is_dir() or not ID_RE.fullmatch(path.name):continue
        summary=load(path/'capture_summary.json');worker=load(path/'status.json');pending=load(path/'qc_pending.json');applied=load(path/'qc_applied.json')
        if worker.get('state') not in TERMINAL or worker.get('recording_active') is True or not (path/'stop_gate.json').is_file():continue
        if not (path/'native_capture_summary.json').is_file():continue
        meta=load(path/'meta.json');stopped=summary.get('stop_unix_ns',0)/1e9 or load(path/'stop_gate.json').get('stop_unix_ns',0)/1e9
        if stopped<=0 or stopped>cutoff:continue
        capture_day=datetime.datetime.fromtimestamp(stopped,datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        report=load(path/'qc_report.json');mode=None
        if pending and not applied and pending.get('created_at',0)<=cutoff:mode='qc'
        elif (day=='*' or capture_day==day) and summary.get('status')=='success' and worker.get('state')=='success' and report.get('level') in ('INFO','WARN') and report.get('scope','full')=='full':mode='archive'
        if request.get('manual_handoff') is True:
            # Only saved terminal captures. Native errors/cancelled takes are
            # not turned into transferable, confirmed episodes by this flag.
            if pending or applied or (summary.get('status')=='success' and report):mode='handoff'
        if not mode:continue
        total=0
        for parent,dirs,files in os.walk(path):
            dirs[:]=[d for d in dirs if not Path(parent,d).is_symlink()]
            for name in files:
                try:total+=Path(parent,name).stat().st_size
                except OSError:pass
        selection=meta.get('reference_selection') or {}
        items.append(dict(session_id=path.name,path=str(path),bytes=total,mode=mode,day=capture_day,
            level=report.get('level'),request_id=pending.get('request_id'),native_sha256=hashlib.sha256((path/'native_capture_summary.json').read_bytes()).hexdigest(),
            reference=meta.get('reference_image'),reference_sha256=meta.get('reference_sha256'),
            task_name=(selection.get('snapshot') or {}).get('name') or (selection.get('list') or {}).get('name') or '',
            reference_name=(selection.get('asset') or {}).get('name',''),stop_time=stopped))
    return dict(device_id=identity(),root=str(root),volume=str(volume),state=status.get('state'),current_session=status.get('session'),
                task_list=status.get('task_list'),items=items)

def guarded_mark(image,path,status):
    """Update the matching attempt, without overwriting a later take's reservation."""
    image=Path(image).resolve();image.relative_to(ledger.LIBRARY.resolve());target=image.parent/'used.yaml'
    with (B/'runtime/ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);book=ledger.read(target)
        if target.exists():
            backup=B/'runtime/ledger_backups';backup.mkdir(exist_ok=True)
            (backup/(hashlib.sha256(str(target).encode()).hexdigest()[:16]+'_'+str(time.time_ns())+'_host-qc.yaml')).write_bytes(target.read_bytes())
        summary=load(Path(path)/'capture_summary.json');quality=summary.get('qc_level') or ('ERROR' if status=='error' else 'INFO')
        attempts=book.setdefault('attempts',{}).setdefault(image.name,[])
        attempt=next((v for v in attempts if v.get('session')==str(path)),None)
        item=dict(session=str(path),at=time.time(),status=status,qc_level=quality,has_data=True,deleted=False,retained=True,
                  server_qc=True,retake_required=status=='error')
        report=load(Path(path)/'qc_report.json');issues=[]
        for name,stream in report.get('streams',{}).items():
            issues.extend(dict(modality=name,check=v.get('check'),message=v.get('message'),level=v.get('level')) for v in stream.get('findings',[]) if v.get('level')=='ERROR')
        issues.extend(dict(modality='session',check=v.get('check'),message=v.get('message'),level=v.get('level')) for v in report.get('findings',[]) if v.get('level')=='ERROR')
        item['qc_reasons']=issues[:40]
        if attempt is None:attempts.append(item)
        else:attempt.update(item)
        used=book.setdefault('used',{});existing=used.get(image.name)
        owns=isinstance(existing,dict) and str(existing.get('session',''))==str(path)
        other=existing is not None and not owns and ledger.valid_used(existing)
        if not other:
            if status=='success':
                used[image.name]=dict(session=str(path),at=time.strftime('%Y-%m-%d %H:%M:%S'),status='success',qc_level=quality)
                book.get('failed',{}).pop(image.name,None)
            else:
                if owns:used.pop(image.name,None)
                book.setdefault('failed',{})[image.name]=dict(session=str(path),at=time.strftime('%Y-%m-%d %H:%M:%S'),status='error',retake_required=True)
        ledger.atomic_yaml(target,book)

def apply(request):
    from qc_deferred import apply_result
    identifier=request['session_id'];path=session(identifier);finalized(path)
    resultdir=Path('/home/orangepi/maintenance/server-qc/results')/identifier
    if resultdir.is_symlink() or resultdir.resolve().parent!=Path('/home/orangepi/maintenance/server-qc/results').resolve():raise ValueError('Invalid result destination')
    result=load(resultdir/'result.json');meta=load(path/'meta.json');reference=meta.get('reference_image')
    expected=meta.get('reference_sha256')
    if reference:
        image=(B.parent/reference).resolve();image.relative_to(ledger.LIBRARY.resolve())
        if not expected or not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Frozen reference image changed; no task mark or cleanup permitted')
    if result.get('session_id')!=identifier:raise ValueError('Wrong result session')
    previous=ledger.mark;ledger.mark=guarded_mark
    try:receipt=apply_result(resultdir,path.parent,B)
    finally:ledger.mark=previous
    # The host's INFO/WARN level is not enough to archive a success. The Pi
    # application step also rechecks its frozen full-QC/data receipt; if that
    # local check rejects, make the SSH protocol fail so the service cannot
    # continue to archive and clean up based on the host's stale level alone.
    if result.get('level') in ('INFO','WARN') and receipt.get('passed') is not True:
        raise RuntimeError('Pi rejected the full-QC qualification; original session retained')
    atomic(STATE/'results'/ (identifier+'.json'),dict(receipt,reference=reference,reference_sha256=expected,
         retake_required=not receipt['passed'],server_path=request.get('server_path'),qc_report=load(path/'qc_report.json')))
    return receipt

def signable(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def prepare_results(request):
    """Create only the validated closed session's return path, never arbitrary SSH paths."""
    identifier=request['session_id'];path=session(identifier);finalized(path)
    root=RESULTS
    for directory in [root.parent,root]:
        if directory.is_symlink():raise ValueError('QC return parent is a symlink')
        directory.mkdir(parents=True,exist_ok=True)
    target=root/identifier
    if target.is_symlink():raise ValueError('QC result target is a symlink')
    target.mkdir(exist_ok=True)
    if target.resolve().parent!=root.resolve():raise ValueError('QC return path escaped its root')
    return dict(device_id=identity(),session_id=identifier,result_directory=str(target),ready=True)

def cleanup(request):
    signed=request['receipt'];signature=signed.get('signature','');receipt={k:v for k,v in signed.items() if k!='signature'}
    if receipt.get('purpose') not in (None,'legacy_qc_archive_v1'):raise ValueError('Not a legacy QC cleanup receipt')
    key=(B/'private/server_qc_hmac').read_bytes()
    if not hmac.compare_digest(hmac.new(key,signable(receipt),hashlib.sha256).hexdigest(),signature):raise ValueError('Invalid server archive signature')
    identifier=receipt['session_id'];expected=receipt['manifest']
    if receipt.get('device_id')!=identity() or expected.get('device_id')!=identity() or expected.get('session_id')!=identifier:
        raise ValueError('Archive identity mismatch')
    if receipt.get('archive_verified') is not True or not receipt.get('archive_path') or receipt.get('server_id')!=load(STATE/'server_identity.json').get('server_id'):
        raise ValueError('Verified server archive proof missing')
    root,volume=roots()
    if expected.get('root')!=str(root) or not ID_RE.fullmatch(identifier):raise ValueError('Archive root or session mismatch')
    record=STATE/'archives'/(identifier+'.json')
    with session_lock(B,identifier):
        previous=load(record)
        if previous.get('state')=='deleted':
            if previous.get('receipt_signature')!=signature:raise ValueError('Different deletion receipt was already used')
            return previous
        idle();path=root/identifier;trashroot=volume/'.server-qc-trash'
        if trashroot.is_symlink():raise ValueError('Trash directory must not be a link')
        trashroot.mkdir(exist_ok=True)
        if trashroot.resolve().parent!=volume:raise ValueError('Trash directory escaped the configured volume')
        recovering=previous.get('state')=='deleting'
        if recovering:
            if previous.get('receipt_signature')!=signature:raise ValueError('Different in-progress receipt')
            target=Path(previous['trash_path'])
            if target.is_symlink() or target.resolve().parent!=trashroot.resolve():raise ValueError('Trash identity invalid')
            if path.exists():raise ValueError('Original name was reused; manual check required')
        else:
            path=session(identifier);finalized(path)
            if path.stat().st_dev!=expected['source_dev'] or path.stat().st_ino!=expected['source_inode']:raise ValueError('Session directory was replaced')
            applied=load(path/'qc_applied.json')
            if receipt.get('result_sha256'):
                if applied.get('result_sha256')!=receipt['result_sha256'] or applied.get('passed')!=receipt['passed']:
                    raise ValueError('Pi QC acknowledgement missing or changed')
            else:
                qc=load(path/'qc_report.json')
                if qc.get('level') not in ('INFO','WARN') or qc.get('scope','full')!='full' or receipt.get('passed') is not True or load(path/'capture_summary.json').get('status')!='success' or load(path/'status.json').get('state')!='success':raise ValueError('Successful local full-QC proof missing')
            actual=source_manifest(path,progress=idle_progress())
            if not same_manifest(expected,actual):raise ValueError('Pi file tree differs from fully verified server copy')
            finalized(path);target=trashroot/(identifier+'-'+uuid.uuid4().hex)
            previous=dict(version=1,state='deleting',session_id=identifier,device_id=identity(),
                          archive_path=receipt['archive_path'],server_id=receipt['server_id'],passed=receipt['passed'],
                          manifest_digest=expected['digest'],receipt_signature=signature,trash_path=str(target),started_at=time.time())
            atomic(record,previous);path.rename(target);sync_dir(root);sync_dir(trashroot)
        if target.exists():
            if target.is_symlink() or target.resolve().parent!=trashroot.resolve():raise ValueError('Deletion boundary invalid')
            actual=source_manifest(target,hashes=recovering,progress=idle_progress())
            if not same_manifest(expected,actual,hashes=recovering):raise ValueError('Quarantined files changed; retaining them')
            idle();shutil.rmtree(str(target));sync_dir(trashroot)
        previous.update(state='deleted',deleted_at=time.time());atomic(record,previous)
        return previous

def _handoff_key():return (B/'private/server_qc_hmac').read_bytes()

def _handoff_record(identifier):
    if not isinstance(identifier,str) or not ID_RE.fullmatch(identifier):raise ValueError('Invalid handoff id')
    return STATE/'handoffs'/(identifier+'.json')

def _capture_lease():
    # The active profile owns capture.lock, while custody records are shared.
    path=project_paths.BASE/'runtime/capture.lock';path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a')
    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BaseException:handle.close();raise
    return handle

def _remaining_handoff_files(target,expected):
    """After a committed delete began, missing files are valid crash recovery."""
    wanted={row['path']:row for row in expected['files']}
    for parent,dirs,names in os.walk(target,followlinks=False):
        if any(Path(parent,n).is_symlink() for n in dirs):raise ValueError('Link in handoff trash')
        for name in names:
            path=Path(parent,name);relative=path.relative_to(target).as_posix();row=wanted.get(relative)
            if row is None or path.is_symlink() or not path.is_file():raise ValueError('Unexpected handoff trash file')
            if path.stat().st_size!=row['bytes']:raise ValueError('Handoff trash size changed')
            digest=hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
            if digest.hexdigest()!=row['sha256']:raise ValueError('Handoff trash SHA changed')

def handoff_status(request):
    identifier=request['session_id'];record=load(_handoff_record(identifier))
    if record and record.get('receipt_sha256')!=request.get('handoff_receipt_sha256'):
        raise ValueError('Wrong custody transaction query')
    return dict(device_id=identity(),session_id=identifier,state=record.get('state','missing'),
        receipt_sha256=record.get('receipt_sha256'),deleted_at=record.get('deleted_at'))

def _handoff_roots(source_root):
    current_root,current_volume=roots()
    if str(current_root)==source_root:return current_root,current_volume,project_paths.PROJECT
    current=storage_paths.config()
    configs=[B/'configs/storage.yaml']+[B.parent/'profiles'/mode/'orangepi/configs/storage.yaml' for mode in ('emg','eeg')]
    for path in configs:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024:continue
        cfg=yaml.safe_load(path.read_text())
        if not isinstance(cfg,dict) or cfg.get('mode')!='sd':continue
        if (cfg.get('mount_point')!=current.get('mount_point')
                or str(cfg.get('expected_uuid','')).upper()!=str(current.get('expected_uuid','')).upper()):continue
        fields=[cfg.get(key) for key in ('directory','device_id','shift')]
        if any(not isinstance(v,str) or not v or Path(v).name!=v or v in ('.','..') for v in fields):continue
        volume=Path(cfg['mount_point']).resolve()/cfg['directory']/cfg['device_id']
        mode=cfg.get('capture_profile')
        if mode is not None:
            if mode not in ('emg','eeg'):continue
            volume=volume/'profiles'/mode
        root=volume/'data'/cfg['shift']
        if root.resolve()!=root or volume.resolve()!=volume:continue
        if str(root)==source_root:return root,volume,path.parent.parent.parent
    raise ValueError('Handoff root is not an explicitly configured capture profile')

def _handoff_session(root,identifier):
    path=root/identifier
    if path.is_symlink() or not path.is_dir() or path.resolve().parent!=root:
        raise ValueError('Frozen handoff session missing or changed')
    return path

def raw_handoff(request):
    signed=request['receipt'];root,volume,reference_project=_handoff_roots(signed.get('manifest',{}).get('root'))
    receipt=validate_raw_receipt(signed,_handoff_key(),identity(),
        load(STATE/'server_identity.json').get('server_id'),root)
    identifier=receipt['session_id'];record=_handoff_record(identifier)
    with session_lock(B,identifier),_capture_lease():
        previous=load(record)
        if previous:
            if previous.get('receipt_signature')!=signed['signature']:raise ValueError('Conflicting handoff receipt')
            if previous.get('state')=='deleted':return previous
        idle();trash=volume/'.server-handoff-trash'
        if trash.is_symlink():raise ValueError('Handoff trash must not be a link')
        trash.mkdir(exist_ok=True)
        if trash.resolve().parent!=volume:raise ValueError('Invalid handoff trash boundary')
        target=trash/(identifier+'-'+receipt['transaction_id']);path=root/identifier
        if target.is_symlink():raise ValueError('Invalid detached handoff path')
        if path.exists() and target.exists():raise ValueError('Original handoff name was reused')
        if not previous:
            path=_handoff_session(root,identifier);finalized(path)
            from required_device_contract import resolve_selection
            frozen_selection=load(path/'device_selection.json')
            contract,contract_errors,legacy=resolve_selection(frozen_selection.get('device_config'),frozen_selection)
            if contract_errors or contract is None or legacy:
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 冻结必需设备合同缺失，原件未交接；升级主机协议后重试')
            meta=load(path/'meta.json')
            if not isinstance(meta,dict):raise ValueError('QC_HOST_UPGRADE_REQUIRED: 冻结本机metadata无效，原件未交接')
            pending=load(path/'qc_pending.json')
            from capture_qc_receipt import verify_input_signature,verify_portable_input_signature,portable_input_signature,verify as verify_qc_receipt
            if pending:
                if (not isinstance(pending.get('input_signature'),str)
                        or not isinstance(pending.get('local_input_signature'),str)
                        or pending.get('session_id')!=identifier):
                    raise ValueError('QC_HOST_UPGRADE_REQUIRED: 手动交接缺少原始输入绑定；原件未交接，请升级主机协议后重试')
                source_input_signature=pending['input_signature'];source_local_input_signature=pending['local_input_signature']
                request_id=pending.get('request_id');require_marker=False
                if (not verify_input_signature(path,source_local_input_signature,contract,require_marker=False)
                        or not verify_portable_input_signature(path,source_input_signature,contract,require_marker=False)):
                    raise ValueError('QC_HOST_UPGRADE_REQUIRED: 待质检输入已变化；原件未交接，请先复核后重试')
            else:
                source_receipt=load(path/'capture_qc_receipt.json')
                receipt_errors=verify_qc_receipt(path)
                if receipt_errors or not isinstance(source_receipt.get('input_signature'),str):
                    raise ValueError('QC_HOST_UPGRADE_REQUIRED: 既有完整质检回执无效；原件未交接，请升级主机协议后重试')
                source_local_input_signature=source_receipt['input_signature']
                if not verify_input_signature(path,source_local_input_signature,contract,require_marker=True):
                    raise ValueError('QC_HOST_UPGRADE_REQUIRED: 既有完整质检输入已变化；原件未交接')
                source_input_signature=portable_input_signature(path,contract,require_marker=True)
                request_id=None;require_marker=True
            expected_ref=meta.get('reference_sha256')
            if not isinstance(expected_ref,str) or not expected_ref or (pending and pending.get('reference_sha256')!=expected_ref):
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 冻结参考身份缺失；原件未交接，请升级主机协议后重试')
            required_version=contract.get('version')
            if (receipt.get('qc_protocol_version')!=2
                    or receipt.get('qc_qualification_protocol')!='required-device-contract-v1'
                    or receipt.get('input_signature_kind')!='portable-content-sha256-v1'
                    or receipt.get('input_signature')!=source_input_signature
                    or receipt.get('local_input_signature')!=source_local_input_signature
                    or receipt.get('reference_sha256')!=expected_ref
                    or receipt.get('required_device_contract_version')!=required_version
                    or not isinstance(receipt.get('qc_request_id'),str)
                    or not receipt.get('qc_request_id')
                    or (pending and receipt.get('qc_request_id')!=request_id)):
                raise ValueError('QC_HOST_UPGRADE_REQUIRED: 主机未证明新版QC输入与资格协议；原件未交接')
            if not pending:request_id=receipt['qc_request_id']
            actual=source_manifest(path,progress=idle_progress())
            if not same_file_bytes(receipt['manifest'],dict(actual,native_sha256=hashlib.sha256((path/'native_capture_summary.json').read_bytes()).hexdigest())):
                raise ValueError('Raw tree changed before handoff')
            if hashlib.sha256((path/'native_capture_summary.json').read_bytes()).hexdigest()!=receipt['manifest']['native_sha256']:
                raise ValueError('Raw capture identity changed')
            request_id=receipt.get('qc_request_id')
            if pending and pending.get('request_id')!=request_id:raise ValueError('QC request identity changed')
            meta=load(path/'meta.json')
            previous=dict(version=1,purpose=RAW_PURPOSE,state='prepared',session_id=identifier,
                device_id=identity(),server_id=receipt['server_id'],receipt_signature=signed['signature'],
                receipt_sha256=sha_value(signed),receipt=signed,trash_path=str(target),source_path=str(path),
                source_meta=meta,reference_project=str(reference_project),
                qc_request_id=request_id,native_sha256=receipt['manifest']['native_sha256'],
                frozen_selection=frozen_selection,source_summary=load(path/'capture_summary.json'),
                start_gate=load(path/'start_gate.json'),stop_gate=load(path/'stop_gate.json'),
                source_pending=pending,source_input_signature=source_input_signature,
                source_local_input_signature=source_local_input_signature,qc_require_marker=require_marker,
                qc_protocol_version=receipt['qc_protocol_version'],
                qc_qualification_protocol=receipt['qc_qualification_protocol'],started_at=time.time())
            atomic(record,previous)
        if path.exists():
            finalized(path)
            if not same_file_bytes(receipt['manifest'],dict(source_manifest(path,progress=idle_progress()),native_sha256=hashlib.sha256((path/'native_capture_summary.json').read_bytes()).hexdigest())):
                raise ValueError('Raw tree changed during handoff recovery')
            path.rename(target);sync_dir(root);sync_dir(trash)
        if not target.exists() and previous.get('state')!='deleting_files':
            raise ValueError('Detached raw tree missing before committed deletion')
        if target.exists():
            if previous.get('state')=='deleting_files':_remaining_handoff_files(target,receipt['manifest'])
            else:
                if not same_file_bytes(receipt['manifest'],dict(source_manifest(target,progress=idle_progress()),native_sha256=hashlib.sha256((target/'native_capture_summary.json').read_bytes()).hexdigest())):
                    raise ValueError('Detached raw tree changed')
                previous.update(state='deleting_files');atomic(record,previous)
            idle();shutil.rmtree(str(target));sync_dir(trash)
        previous.update(state='deleted',deleted_at=time.time());atomic(record,previous)
        return previous

def prepare_handoff_results(request):
    idle();identifier=request['session_id'];record=load(_handoff_record(identifier))
    if record.get('purpose')!=RAW_PURPOSE or record.get('state')!='deleted':raise ValueError('No completed raw handoff')
    if record.get('receipt_sha256')!=request.get('handoff_receipt_sha256'):raise ValueError('Wrong raw handoff')
    for parent in [RESULTS.parent,RESULTS,RESULTS/identifier]:
        if parent.is_symlink():raise ValueError('Linked handoff result destination')
        parent.mkdir(parents=True,exist_ok=True)
    return dict(device_id=identity(),session_id=identifier,result_directory=str(RESULTS/identifier),ready=True)

def _handoff_result_qualification(record,envelope,result,resultdir,report):
    """Require a result tied to the exact handoff inputs and complete report."""
    from required_device_contract import resolve_selection
    selection=record.get('frozen_selection');pending=record.get('source_pending') or {}
    contract,errors,legacy=resolve_selection((selection or {}).get('device_config'),selection)
    if errors or contract is None or legacy:raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff device contract is invalid')
    raw_receipt=record.get('receipt') if isinstance(record.get('receipt'),dict) else {}
    reference=record.get('source_meta',{}).get('reference_sha256')
    receipt_sha=envelope.get('qualification_receipt_sha256') if isinstance(envelope,dict) else None
    result_path=Path(resultdir)/'result.json'
    result_sha=hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path.is_file() else None
    if (envelope.get('session_id')!=record.get('session_id')
            or envelope.get('device_id')!=record.get('device_id')
            or envelope.get('server_id')!=record.get('server_id')
            or envelope.get('handoff_receipt_sha256')!=record.get('receipt_sha256')
            or envelope.get('result_sha256')!=result_sha
            or not isinstance(envelope.get('server_path'),str)
            or not Path(envelope['server_path']).is_absolute()
            or envelope.get('qc_protocol_version')!=2
            or envelope.get('qc_qualification_protocol')!='required-device-contract-v1'
            or envelope.get('passed') is not True
            or not isinstance(receipt_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',receipt_sha)
            or envelope.get('input_signature')!=record.get('source_input_signature')
            or envelope.get('local_input_signature')!=record.get('source_local_input_signature')
            or envelope.get('checked_input_signature')!=result.get('checked_input_signature')
            or envelope.get('reference_sha256')!=reference
            or raw_receipt.get('input_signature')!=record.get('source_input_signature')
            or raw_receipt.get('local_input_signature')!=record.get('source_local_input_signature')
            or raw_receipt.get('reference_sha256')!=reference
            or pending.get('request_id')!=record.get('qc_request_id')
            or result.get('input_signature')!=record.get('source_input_signature')
            or result.get('local_input_signature')!=record.get('source_local_input_signature')
            or result.get('request_id')!=record.get('qc_request_id')
            or result.get('reference_sha256')!=reference
            or not isinstance(result.get('checked_input_signature'),str)
            or not re.fullmatch(r'[0-9a-f]{64}',result.get('checked_input_signature',''))):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff result is not bound to the committed source inputs')
    if (result.get('level') not in ('INFO','WARN') or result.get('passed') is not True
            or report.get('scope','full')!='full' or report.get('level')!=result.get('level')
            or not isinstance(report.get('session_dir'),str)
            or Path(report['session_dir']).name!=record.get('session_id')):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff full-QC report is incomplete')
    start=record.get('start_gate');stop=record.get('stop_gate');summary=record.get('source_summary')
    window=report.get('window')
    if (not isinstance(start,dict) or not isinstance(stop,dict) or not isinstance(summary,dict)
            or not isinstance(window,dict) or type(window.get('t0')) not in (int,float)
            or type(window.get('t1')) not in (int,float)):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff START/STOP or report window is missing')
    t0=summary.get('start_unix_ns');t1=summary.get('stop_unix_ns')
    if (type(t0) is not int or type(t1) is not int or t1<=t0
            or abs(window['t0']-t0/1e9)>1e-6 or abs(window['t1']-t1/1e9)>1e-6
            or start.get('start_unix_ns')!=t0 or stop.get('stop_unix_ns')!=t1
            or stop.get('recording_active') is not False):
        raise ValueError('Handoff QC window differs from frozen START/STOP facts')
    streams=report.get('streams');required=set(contract.get('required_devices',()))
    if not isinstance(streams,dict) or not required.issubset(streams):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff report does not cover all required devices')
    if any(not isinstance(streams[name],dict) or streams[name].get('level') not in ('INFO','WARN') for name in required):
        raise ValueError('Handoff required stream QC did not pass')
    manifest=record.get('receipt',{}).get('manifest',{});files=manifest.get('files',[])
    by_path={row.get('path'):row for row in files if isinstance(row,dict)}
    for name in required:
        detail=summary.get(name)
        if not isinstance(detail,dict):raise ValueError('Handoff source summary lacks '+name)
        if name.startswith('cam_ros_') or name.startswith('cam_'):
            count=detail.get('frames')
            needed=(name+'/frames.mp4',name+'/timestamps.csv')
        elif name.startswith('emg_'):
            count=detail.get('emg_frames')
            needed=()
            if not any(path.startswith(name+'/') and (path.endswith('.npz')) for path in by_path):
                raise ValueError('Handoff manifest lacks '+name+' sample arrays')
        elif name=='board_imu':
            count=detail.get('frames');needed=()
            if not any(name+'/'+candidate in by_path for candidate in ('raw.csv','board_imu.npz')):
                raise ValueError('Handoff manifest lacks board IMU data')
        elif name=='eeg':
            count=(result.get('derived_counts') or {}).get('eeg_samples')
            needed=()
            if not isinstance(count,int) or count<=0 or not any(path.startswith(name+'/') and path.endswith('.npz') for path in by_path):
                raise ValueError('Handoff manifest lacks finalized EEG data')
        else:raise ValueError('Unknown required device in handoff contract')
        if not isinstance(count,int) or count<=0:raise ValueError('Handoff required stream has no recorded samples: '+name)
        for path in needed:
            if path not in by_path or type(by_path[path].get('bytes')) is not int or by_path[path]['bytes']<=0:
                raise ValueError('Handoff manifest lacks required data file: '+path)
    if 'eeg' in required:
        final=resultdir/'artifacts/eeg/gate_finalization.json'
        if final.is_symlink() or not final.is_file():raise ValueError('QC_HOST_UPGRADE_REQUIRED: missing EEG STOP finalization proof')
        proof=load(final);gates=proof.get('gate_sha256') if isinstance(proof,dict) else None
        if not isinstance(gates,dict):raise ValueError('EEG STOP finalization proof is invalid')
        start_row=by_path.get('start_gate.json',{});stop_row=by_path.get('stop_gate.json',{})
        start_sha=gates.get('start');stop_sha=gates.get('stop')
        if (proof.get('schema_version')!=1 or proof.get('policy')!='whole_sdk_batches_final_controller_gate_v1'
                or proof.get('start_monotonic_ns')!=start.get('start_monotonic_ns')
                or proof.get('stop_monotonic_ns')!=stop.get('stop_monotonic_ns')
                or not isinstance(start_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',start_sha)
                or not isinstance(stop_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',stop_sha)
                or start_sha!=start_row.get('sha256') or stop_sha!=stop_row.get('sha256')):
            raise ValueError('EEG STOP finalization proof is invalid')
        derived=result.get('derived_counts')
        if not isinstance(derived,dict) or derived.get('eeg_samples')!=proof.get('kept_samples'):
            raise ValueError('EEG finalized count differs from QC output')
    return contract

def _apply_handoff_result(request):
    envelope=verify_signature(request['receipt'],_handoff_key(),RESULT_PURPOSE)
    identifier=envelope.get('session_id');record=load(_handoff_record(identifier));idle()
    if (envelope.get('device_id')!=identity() or envelope.get('server_id')!=record.get('server_id')
            or record.get('state')!='deleted' or envelope.get('handoff_receipt_sha256')!=record.get('receipt_sha256')):
        raise ValueError('QC result does not belong to this completed handoff')
    resultdir=RESULTS/identifier
    if resultdir.is_symlink() or resultdir.resolve().parent!=RESULTS.resolve():raise ValueError('Invalid result directory')
    result=load(resultdir/'result.json');digest=hashlib.sha256((resultdir/'result.json').read_bytes()).hexdigest()
    if (digest!=envelope.get('result_sha256') or result.get('session_id')!=identifier
            or result.get('request_id')!=record.get('qc_request_id')
            or result.get('native_sha256')!=record.get('native_sha256')
            or result.get('level') not in ('INFO','WARN','ERROR')):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: signed host result identity or checksum mismatch')
    expected_pass=result.get('level') in ('INFO','WARN')
    raw_receipt=record.get('receipt') if isinstance(record.get('receipt'),dict) else {}
    source_meta=record.get('source_meta') if isinstance(record.get('source_meta'),dict) else {}
    reference_sha=source_meta.get('reference_sha256')
    qualification_sha=envelope.get('qualification_receipt_sha256')
    if (type(envelope.get('passed')) is not bool or envelope['passed'] is not expected_pass
            or result.get('passed') is not expected_pass
            or envelope.get('qc_protocol_version')!=2
            or envelope.get('qc_qualification_protocol')!='required-device-contract-v1'
            or envelope.get('input_signature')!=record.get('source_input_signature')
            or envelope.get('local_input_signature')!=record.get('source_local_input_signature')
            or envelope.get('input_signature')!=raw_receipt.get('input_signature')
            or envelope.get('local_input_signature')!=raw_receipt.get('local_input_signature')
            or envelope.get('reference_sha256')!=reference_sha
            or result.get('reference_sha256')!=reference_sha
            or envelope.get('checked_input_signature')!=result.get('checked_input_signature')
            or not isinstance(envelope.get('server_path'),str)
            or not Path(envelope['server_path']).is_absolute()
            or (expected_pass and (not isinstance(qualification_sha,str)
                or not re.fullmatch(r'[0-9a-f]{64}',qualification_sha)))
            or (not expected_pass and qualification_sha is not None)):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: signed host result does not bind its qualification to the handoff')
    output=STATE/'results'/(identifier+'.json');previous=load(output)
    if previous:
        if (previous.get('result_sha256')!=digest or previous.get('passed') is not envelope['passed']
                or previous.get('qualification_receipt_sha256')!=qualification_sha
                or previous.get('handoff_result_receipt')!=request['receipt']):
            raise ValueError('Conflicting post-handoff QC result')
        return previous
    artifacts=result.get('artifacts',{})
    if 'qc_report.json' not in artifacts:raise ValueError('QC report missing')
    for relative,sha in artifacts.items():
        p=Path(relative);source=resultdir/'artifacts'/p
        if (p.is_absolute() or '..' in p.parts or source.is_symlink()
                or (resultdir/'artifacts').resolve() not in source.resolve().parents
                or hashlib.sha256(source.read_bytes()).hexdigest()!=sha):raise ValueError('QC artifact changed')
    report=load(resultdir/'artifacts/qc_report.json')
    if report.get('level')!=result['level']:raise ValueError('Result/report level mismatch')
    retake=not expected_pass
    if expected_pass:
        qualified_contract=_handoff_result_qualification(record,envelope,result,resultdir,report)
        if not isinstance(qualified_contract,dict):raise ValueError('QC_HOST_UPGRADE_REQUIRED: handoff qualification is invalid')
    meta=source_meta;reference=meta.get('reference_image')
    if expected_pass and (not isinstance(reference,str) or not reference):
        raise ValueError('QC_HOST_UPGRADE_REQUIRED: successful handoff lacks a frozen reference identity')
    if reference:
        project=Path(record['reference_project']).resolve()
        allowed=[B.parent.resolve(),(B.parent/'profiles/emg').resolve(),(B.parent/'profiles/eeg').resolve()]
        if project not in allowed:raise ValueError('Unknown frozen reference project')
        image=(project/reference).resolve();image.relative_to((project/'configs/environments').resolve())
        if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest()!=meta.get('reference_sha256'):
            raise ValueError('Frozen reference changed; result retained without ledger overwrite')
        target=image.parent/'used.yaml';base=project/'orangepi';(base/'runtime').mkdir(exist_ok=True)
        with (base/'runtime/ledger.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);book=ledger.read(target)
            if target.exists():
                backup=base/'runtime/ledger_backups';backup.mkdir(exist_ok=True)
                (backup/(hashlib.sha256(str(target).encode()).hexdigest()[:16]+'_'+str(time.time_ns())+'_handoff.yaml')).write_bytes(target.read_bytes())
            attempts=book.setdefault('attempts',{}).setdefault(image.name,[])
            item=next((v for v in attempts if v.get('session')==record['source_path']),None)
            if item is None:item={'session':record['source_path']};attempts.append(item)
            item.update(status='error' if retake else 'success',qc_level=result['level'],server_qc=True,
                has_data=True,deleted=False,retained=True,data_location='host',pi_original_deleted=True,
                server_path=envelope.get('server_path'),retake_required=retake,
                result_sha256=digest,qualification_receipt_sha256=qualification_sha,
                handoff_receipt_sha256=record.get('receipt_sha256'),reference_sha256=reference_sha)
            used=book.setdefault('used',{});owner=used.get(image.name)
            other=isinstance(owner,dict) and owner.get('session')!=record['source_path'] and ledger.valid_used(owner)
            if not other:
                if retake:
                    if isinstance(owner,dict) and owner.get('session')==record['source_path']:used.pop(image.name,None)
                    book.setdefault('failed',{})[image.name]=dict(session=record['source_path'],status='error',retake_required=True)
                else:
                    used[image.name]=dict(session=record['source_path'],status='success',qc_level=result['level'],
                        data_location='host',server_qc=True,pi_original_deleted=True,retained=True,
                        server_path=envelope.get('server_path'),result_sha256=digest,
                        qualification_receipt_sha256=qualification_sha,
                        handoff_receipt_sha256=record.get('receipt_sha256'),reference_sha256=reference_sha)
                    book.get('failed',{}).pop(image.name,None)
            ledger.atomic_yaml(target,book)
    value=dict(version=1,purpose=RESULT_PURPOSE,session_id=identifier,result_sha256=digest,level=result['level'],
        raw_level=result.get('raw_level'),passed=not retake,retake_required=retake,applied_at=time.time(),
        server_path=envelope.get('server_path'),data_location='host',pi_original_deleted=True,
        qc_protocol_version=envelope.get('qc_protocol_version'),
        qc_qualification_protocol=envelope.get('qc_qualification_protocol'),
        input_signature=envelope.get('input_signature'),local_input_signature=envelope.get('local_input_signature'),
        checked_input_signature=envelope.get('checked_input_signature'),reference_sha256=reference_sha,
        qualification_receipt_sha256=qualification_sha,handoff_receipt_sha256=record.get('receipt_sha256'),
        request_id=result.get('request_id'),native_sha256=result.get('native_sha256'),
        handoff_result_receipt=request['receipt'])
    atomic(output,value);atomic(project_paths.BASE/'runtime/qc_results_revision.json',dict(at=time.time(),session_id=identifier))
    return value

def apply_handoff_result(request):
    identifier=request.get('receipt',{}).get('session_id','')
    with session_lock(B,identifier):return _apply_handoff_result(request)

def main():
    request=json.load(sys.stdin)
    if request.get('device_id')!=identity():raise ValueError('Wrong Pi identity')
    action=request['action']
    if action=='scan':value=scan(request)
    elif action=='status':
        status=current();value=dict(device_id=identity(),**{k:status.get(k) for k in ['state','session','worker']})
    elif action=='manifest':value=manifest(request['session_id'])
    elif action=='apply':value=apply(request)
    elif action=='prepare_results':value=prepare_results(request)
    elif action=='cleanup':value=cleanup(request)
    elif action=='raw_handoff':value=raw_handoff(request)
    elif action=='prepare_handoff_results':value=prepare_handoff_results(request)
    elif action=='apply_handoff_result':value=apply_handoff_result(request)
    elif action=='handoff_status':value=handoff_status(request)
    else:raise ValueError('Unknown action')
    print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc),'kind':type(exc).__name__}),file=sys.stderr,flush=True);sys.exit(1)
