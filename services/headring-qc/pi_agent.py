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
        # The same-filesystem rename quarantines exactly the verified closed tree.
        if target.exists():
            if target.is_symlink() or target.resolve().parent!=trashroot.resolve():raise ValueError('Deletion boundary invalid')
            actual=source_manifest(target,hashes=recovering,progress=idle_progress())
            if not same_manifest(expected,actual,hashes=recovering):raise ValueError('Quarantined files changed; retaining them')
            idle();shutil.rmtree(str(target));sync_dir(trashroot)
        previous.update(state='deleted',deleted_at=time.time());atomic(record,previous)
        return previous

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
    else:raise ValueError('Unknown action')
    print(json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc),'kind':type(exc).__name__}),file=sys.stderr,flush=True);sys.exit(1)
