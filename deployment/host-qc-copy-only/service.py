#!/usr/bin/env python3
"""LAN QC queue: verified copies, unchanged checker rules, acknowledged cleanup."""
import collections,concurrent.futures,contextlib,datetime,hashlib,hmac,http.server,json,os,re,shlex,shutil,sqlite3,subprocess,sys,threading,time,traceback,uuid
from pathlib import Path
from host_storage import require_storage, storage_path
ROOT=Path(__file__).resolve().parent
CONFIG=json.loads((ROOT/'config.json').read_text())
DATA=Path(CONFIG['data_root']);DB=DATA/'state/queue.sqlite3'
STOP=threading.Event();QC_LOCK=threading.Lock();CLAIM_LOCK=threading.Lock()
RECEIVER=None
WORK_CHANGED=threading.Condition();WORK_VERSION=0
TRANSFER_PARALLELISM=max(1,min(8,int(CONFIG.get('transfer_parallelism',4))))
TRANSFER_KIB_S=max(0,int(CONFIG.get('transfer_kib_s',0)))
MAX_ACTIVE_PER_DEVICE=max(1,min(3,int(CONFIG.get('active_per_device',2))))
TRANSFER=threading.Semaphore(TRANSFER_PARALLELISM)
DEVICES={d['id']:d for d in CONFIG['devices']}
PI_IO={d:threading.BoundedSemaphore(2) for d in DEVICES}
# Execute the current protocol so reconnecting devices do not retain a stale scan cutoff.
AGENT='python3 -c '+shlex.quote((ROOT/'pi_agent.py').read_text())
ACTIVE=('copying','verifying','waiting_qc','qc','applying','final_sync','archiving','cleaning')
UPLOAD_ACTIVE=('copying','verifying')
PROCESS_ACTIVE=('waiting_qc','qc','applying','final_sync','archiving','cleaning')
NET_LOCK=threading.Lock();NET_SAMPLES=collections.deque(maxlen=40)
NET_COUNTERS=Path('/sys/class/net')/CONFIG.get('network_interface','enp4s0')/'statistics'

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('w') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path);sync_dir(path.parent)

def sync_dir(path):
    fd=os.open(str(path),os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def load(path,default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return default

@contextlib.contextmanager
def db():
    c=sqlite3.connect(DB,timeout=10);c.row_factory=sqlite3.Row
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback();raise
    finally:c.close()

def initialize():
    require_storage(CONFIG)
    for name in ['state','incoming','originals','work','results','archive','quarantine','receipts','logs']:(DATA/name).mkdir(parents=True,exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,device TEXT NOT NULL,session TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',bytes INTEGER DEFAULT 0,transferred INTEGER DEFAULT 0,progress REAL DEFAULT 0,level TEXT,error TEXT,attempts INTEGER DEFAULT 0,available REAL DEFAULT 0,created REAL,updated REAL,details TEXT NOT NULL,archive_path TEXT,UNIQUE(device,session));
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,status TEXT,error TEXT,updated REAL,details TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL,device TEXT,session TEXT,kind TEXT,message TEXT);''')
        if 'priority' not in {x['name'] for x in c.execute('PRAGMA table_info(jobs)')}:c.execute('ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
        if 'ingested' not in {x['name'] for x in c.execute('PRAGMA table_info(jobs)')}:c.execute('ALTER TABLE jobs ADD COLUMN ingested INTEGER NOT NULL DEFAULT 0')
        c.execute("UPDATE jobs SET state='waiting',available=?,error='服务重启后继续核验',updated=? WHERE state IN (%s)" % ','.join('?' for _ in ACTIVE),(time.time(),time.time(),*ACTIVE))
        for row in c.execute("SELECT id,device,session FROM jobs WHERE ingested=0 AND state<>'done'").fetchall():
            if (DATA/'state/jobs'/row['device']/row['session']/'original-verified.json').is_file():
                c.execute("UPDATE jobs SET ingested=1,state='raw_ready' WHERE id=?",(row['id'],))

def event(device,session,kind,message):
    with db() as c:c.execute('INSERT INTO events(at,device,session,kind,message) VALUES(?,?,?,?,?)',(time.time(),device,session,kind,str(message)[-3000:]))
    print(json.dumps(dict(device=device,session=session,kind=kind,message=str(message)[-500:]),ensure_ascii=False),flush=True)

def update(job,**values):
    values['updated']=time.time()
    with db() as c:c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),job['id']))
    if values.get('state') in ('queued','raw_ready','waiting','done','verified_hold'):wake_workers()

def wake_workers():
    global WORK_VERSION
    with WORK_CHANGED:
        WORK_VERSION+=1;WORK_CHANGED.notify_all()

def ssh(device,command):return ['ssh','-q','-F',str(ROOT/'ssh/config'),device,command]

def friendly_error(exc):
    text=str(exc)
    if 'PI_BUSY' in text:return 'Pi 正在准备或采集，后台处理已暂停，原始数据保留。'
    if any(x in text.lower() for x in ('connection timed out','no route to host','network is unreachable','ssh failed')):return '头环暂不可达，保留原始数据，等待网络恢复后继续。'
    if 'Connection refused' in text or 'HTTP Error 503' in text:return '采集服务尚未就绪，等待恢复；未清理 Pi 原始数据。'
    if isinstance(exc,subprocess.TimeoutExpired) or 'timed out after' in text:return '连接或校验超时，将自动重试；未提前清理 Pi 原始数据。'
    if 'QC_RUNTIME_PENDING' in text:return '正在准备服务器质检环境，Pi 原始数据保留。'
    if 'checksum mismatch' in text or 'file tree differs' in text:return '文件校验未通过，Pi 原始数据仍保留，等待重新核对。'
    return text[-1500:]

def agent(device,action,timeout=120,**values):
    if action not in ('status','scan','manifest'):raise ValueError('Copy-only mode forbids mutating Pi actions')
    guard=PI_IO[device] if action in ('manifest','apply','cleanup','prepare_results') else contextlib.nullcontext()
    with guard:p=subprocess.run(ssh(device,AGENT),input=json.dumps(dict(action=action,device_id=device,**values)),text=True,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError(p.stderr[-2000:] or p.stdout[-1000:] or 'SSH failed')
    return json.loads(p.stdout)

def require_idle(device):
    value=agent(device,'status',timeout=20)
    if value['state'] not in ('device_confirm','idle','failed_wait','exhausted') or value.get('session') or (value.get('worker') or {}).get('recording_active'):
        raise RuntimeError('PI_BUSY: 当前采集优先，稍后继续')

def scan_device(device):
    did=device['id']
    if device.get('enabled',True) is False:
        with db() as c:c.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?)',(did,'offline','历史设备，数据已保留' if device.get('historical_only') else '尚未配置连接，等待接入',time.time(),'{}'))
        return
    try:
        cutoff=time.time() if CONFIG.get('continuous_ingestion') else CONFIG['cutoff']
        day='*' if CONFIG.get('continuous_ingestion') else CONFIG['day']
        result=agent(did,'scan',cutoff=cutoff,day=day,timeout=90)
        if result.get('device_id')!=did:raise ValueError('Connected headring identity mismatch')
        with db() as c:
            available=result.get('state') in ('device_confirm','idle','failed_wait','exhausted') and not result.get('current_session')
            c.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?)',(did,'online' if available else 'busy',None,time.time(),json.dumps(result,ensure_ascii=False)))
            for item in result['items']:
                if item.get('stop_time',0)<device.get('minimum_stop_time',0):continue
                prior=load(DATA/'receipts'/did/(item['session_id']+'.json'))
                if prior and prior.get('archive_verified') is True:
                    signed={k:v for k,v in prior.items() if k!='signature'}
                    expected=hmac.new((ROOT/'secrets'/did).read_bytes(),json.dumps(signed,sort_keys=True,separators=(',',':'),allow_nan=False).encode(),hashlib.sha256).hexdigest()
                    if prior.get('device_id')!=did or prior.get('session_id')!=item['session_id'] or not hmac.compare_digest(expected,prior.get('signature','')):raise ValueError('Stored archive receipt authentication failed')
                    continue
                if CONFIG.get('continuous_ingestion') and item.get('day','')<CONFIG.get('minimum_capture_date','2026-11-01'):continue
                c.execute('INSERT OR IGNORE INTO jobs(device,session,mode,bytes,created,updated,details) VALUES(?,?,?,?,?,?,?)',
                          (did,item['session_id'],item['mode'],item['bytes'],time.time(),time.time(),json.dumps(item,ensure_ascii=False)))
        wake_workers()
    except Exception as exc:
        with db() as c:c.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?)',(did,'offline',friendly_error(exc),time.time(),'{}'))

def scanner():
    while not STOP.is_set():
        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as p:list(p.map(scan_device,CONFIG['devices']))
        STOP.wait(60)

def claim(stage='upload'):
    with CLAIM_LOCK,db() as c:
        c.execute('BEGIN IMMEDIATE')
        states=UPLOAD_ACTIVE if stage=='upload' else PROCESS_ACTIVE
        allowed="j.ingested=0 AND j.state IN ('queued','waiting') AND j.device IN (SELECT id FROM devices WHERE status='online')" if stage=='upload' else "j.ingested=1 AND j.state IN ('raw_ready','waiting') AND (j.device IN (SELECT id FROM devices WHERE status='online') OR (j.mode='qc' AND j.level IS NULL))"
        counts="(SELECT COUNT(*) FROM jobs a WHERE a.device=j.device AND a.state IN (%s))" % ','.join('?' for _ in states)
        sql="SELECT j.* FROM jobs j WHERE "+allowed+" AND j.available<=? AND "+counts+"<? ORDER BY CASE WHEN archive_path IS NOT NULL THEN 0 ELSE 1 END,priority DESC,"+counts+" ASC,attempts,bytes,id LIMIT 1"
        row=c.execute(sql,(time.time(),*states,MAX_ACTIVE_PER_DEVICE,*states)).fetchone()
        if not row:return None
        c.execute("UPDATE jobs SET state=?,updated=?,attempts=attempts+1,error=NULL WHERE id=?",('copying' if stage=='upload' else 'waiting_qc',time.time(),row['id']))
        return dict(row)

def hash_file(path,fsync=False):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
        if fsync:os.fsync(f.fileno())
    return h.hexdigest()

def verify_tree(path,manifest,durable=False):
    path=Path(path)
    if path.is_symlink() or not path.is_dir():raise ValueError('Server destination is not an ordinary directory')
    expected={x['path']:x for x in manifest['files']};actual=set();directories=[]
    for parent,dirs,names in os.walk(path,followlinks=False):
        directories.append(Path(parent))
        if any(Path(parent,d).is_symlink() for d in dirs):raise ValueError('Link in server archive')
        for name in names:
            p=Path(parent,name)
            if p.is_symlink() or not p.is_file():raise ValueError('Non-regular server archive file')
            rel=p.relative_to(path).as_posix();actual.add(rel);row=expected.get(rel)
            if not row or p.stat().st_size!=row['bytes'] or hash_file(p,fsync=durable)!=row['sha256']:
                raise ValueError('Server checksum mismatch: '+rel)
    if actual!=set(expected):raise ValueError('Server archive file set is incomplete or contains extra files')
    if durable:
        for d in sorted(directories,key=lambda p:len(p.parts),reverse=True):sync_dir(d)
    return dict(files=len(actual),bytes=sum(v['bytes'] for v in expected.values()),verified_at=time.time(),method='all_paths_sizes_sha256_and_fsync' if durable else 'all_paths_sizes_sha256')

def rsync(job,source,destination,push=False,delete=False):
    if push or delete:raise ValueError('Copy-only mode forbids remote writes or deletion')
    destination=Path(destination) if not push else str(destination)
    if push:
        prepared=agent(job['device'],'prepare_results',session_id=job['session'],timeout=30)
        if (prepared.get('device_id')!=job['device'] or prepared.get('session_id')!=job['session']
                or prepared.get('result_directory')!=str(destination) or prepared.get('ready') is not True):
            raise ValueError('Pi QC result directory preparation did not match this job')
    if not push:
        destination.mkdir(parents=True,exist_ok=True)
        resolved=destination.resolve()
        if DATA.resolve() not in resolved.parents or resolved==DATA.resolve():raise ValueError('Rsync destination outside managed data root')
        if delete and DATA.joinpath('work').resolve() not in resolved.parents:raise ValueError('Only a dedicated QC work tree may use --delete')
    command=['rsync','-a','--partial','--partial-dir=.rsync-partial','--protect-args','--info=progress2','--stats','--no-inc-recursive','-e','ssh -q -F '+str(ROOT/'ssh/config')]
    if TRANSFER_KIB_S:command.append('--bwlimit='+str(TRANSFER_KIB_S))
    if delete:command.append('--delete')
    if push:command.extend([str(source).rstrip('/')+'/',job['device']+':'+str(destination).rstrip('/')+'/'])
    else:command.extend([job['device']+':'+str(source).rstrip('/')+'/',str(destination).rstrip('/')+'/'])
    log=DATA/'logs'/('%s-%s-transfer.log'%(job['device'],job['session']))
    with PI_IO[job['device']],TRANSFER,log.open('w') as stream:
        require_idle(job['device'])
        began=time.monotonic();began_unix=time.time()
        p=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,text=True,env=dict(os.environ,LC_ALL='C'))
        try:
            while p.poll() is None:
                if STOP.wait(3):p.terminate();raise RuntimeError('Service is stopping; transfer will resume')
                require_idle(job['device'])
                try:
                    tail=log.read_text(errors='replace')[-4000:]
                    matches=re.findall(r'([\d,]+)\s+(\d+)%',tail)
                    if matches:
                        size,percent=matches[-1];update(job,transferred=int(size.replace(',','')),progress=float(percent))
                except OSError:pass
            if p.returncode:raise RuntimeError(log.read_text(errors='replace')[-1500:])
            elapsed=time.monotonic()-began
            text=log.read_text(errors='replace')
            sizes=re.findall(r'Total transferred file size:\s*([\d,]+)\s+bytes',text)
            transferred=int(sizes[-1].replace(',','')) if sizes else None
            record=dict(direction='host_to_pi' if push else 'pi_to_host',source=str(source),destination=str(destination),
                        started_unix=began_unix,elapsed_seconds=elapsed,transferred_bytes=transferred,
                        application_MB_s=transferred/elapsed/1e6 if transferred is not None and elapsed>0 else None)
            metrics=DATA/'state/jobs'/job['device']/job['session']/'transfer-metrics.jsonl';metrics.parent.mkdir(parents=True,exist_ok=True)
            with metrics.open('a') as stream:stream.write(json.dumps(record)+'\n');stream.flush();os.fsync(stream.fileno())
        finally:
            if p.poll() is None:p.terminate();p.wait(timeout=25)

def local_copy(source,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    p=subprocess.run(['cp','-a','--reflink=auto',str(source),str(destination)],capture_output=True,text=True)
    if p.returncode:raise RuntimeError(p.stderr)

def qc_run(job,work,output):
    entry=ROOT/'collector/orangepi/src/orangepi_collect/framework/qc_host.py'
    python=ROOT/'.venv/bin/python'
    if not python.exists() or not (ROOT/'runtime-ready').exists():raise RuntimeError('QC_RUNTIME_PENDING: waiting for the verified checker environment')
    update(job,state='waiting_qc',progress=0)
    with QC_LOCK:
        update(job,state='qc',progress=0)
        log=DATA/'logs'/('%s-%s-qc.log'%(job['device'],job['session']));output.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',ORANGEPI_HOST_QC='1',ORANGEPI_HOST_QC_WORKERS=str(CONFIG['qc_workers']),ORANGEPI_HOST_VIDEO_BACKEND=CONFIG.get('qc_video_backend','cv2'))
        with log.open('w') as f:
            p=subprocess.Popen([str(python),str(entry),'run',str(work),'--results',str(output),'--workers',str(CONFIG['qc_workers'])],stdout=f,stderr=subprocess.STDOUT,env=env)
            completed=threading.Event();wait_errors=[]
            def wait_for_child():
                try:p.wait()
                except Exception as exc:wait_errors.append(exc)
                finally:completed.set()
            threading.Thread(target=wait_for_child,name='qc-child-waiter',daemon=True).start()
            next_progress=0.
            while not completed.wait(.2):
                if STOP.is_set():p.terminate();p.wait(timeout=30);raise RuntimeError('QC stopped and will resume')
                now=time.monotonic()
                if now<next_progress:continue
                next_progress=now+1.
                progress=load(output/'progress.json',{})
                if progress.get('session_id')==job['session']:
                    total=progress.get('total',0);update(job,progress=100*progress.get('completed',0)/total if total else 0)
            if wait_errors:raise RuntimeError('QC child wait failed: '+str(wait_errors[0]))
            if p.returncode:raise RuntimeError('QC execution error: '+log.read_text(errors='replace')[-2500:])
    result=load(output/job['session']/'result.json')
    if not result or result.get('level') not in ('INFO','WARN','ERROR'):raise RuntimeError('Valid full-QC result was not produced')
    return result

def finish_cleanup(job,records,receipt):
    raise ValueError('Copy-only mode never cleans Pi originals')
    # Re-read the actual committed server files before issuing any deletion request.
    verify_tree(storage_path(receipt['archive_path'],CONFIG),receipt['manifest'],durable=True)
    if not load(ROOT/'config.json',CONFIG).get('cleanup_enabled',False):
        update(job,state='verified_hold',progress=100,error='已归档校验并回传；等待清理链路验收')
        return
    require_idle(job['device']);update(job,state='cleaning',progress=0)
    try:
        response=agent(job['device'],'cleanup',receipt=receipt,timeout=1800)
    except RuntimeError as exc:
        if not any(x in str(exc) for x in ('Pi file tree differs from fully verified server copy','Session directory was replaced')):raise
        # exFAT assigns different inodes after a reboot. Reissue only when every
        # fresh source byte still matches the already committed server archive.
        fresh=agent(job['device'],'manifest',session_id=job['session'],timeout=1800)
        if fresh.get('device_id')!=job['device'] or fresh.get('session_id')!=job['session'] or fresh.get('root')!=receipt['manifest'].get('root'):
            raise ValueError('Session identity changed; retaining Pi originals')
        proof=verify_tree(storage_path(receipt['archive_path'],CONFIG),fresh,durable=True)
        atomic(records/'receipt-history'/(str(time.time_ns())+'.json'),receipt)
        revised={k:v for k,v in receipt.items() if k!='signature'}
        revised.update(manifest=fresh,verification=proof,revalidated_at=time.time())
        key=(ROOT/'secrets'/job['device']).read_bytes()
        revised['signature']=hmac.new(key,json.dumps(revised,sort_keys=True,separators=(',',':'),allow_nan=False).encode(),hashlib.sha256).hexdigest()
        receipt=revised
        atomic(records/'final-manifest.json',fresh);atomic(records/'archive-receipt.json',receipt)
        atomic(DATA/'receipts'/job['device']/(job['session']+'.json'),receipt)
        event(job['device'],job['session'],'revalidated','设备重启后已再次核对全部文件，服务器副本一致')
        response=agent(job['device'],'cleanup',receipt=receipt,timeout=1800)
    if response.get('state')!='deleted' or response.get('receipt_signature')!=receipt['signature']:raise RuntimeError('Pi cleanup receipt mismatch')
    atomic(records/'pi-deleted.json',response)
    update(job,state='done',progress=100,transferred=receipt['manifest']['total_bytes'],archive_path=str(storage_path(receipt['archive_path'],CONFIG)),error=None)
    event(job['device'],job['session'],'done','合格已归档' if receipt['passed'] else '不合格已回原任务列表，原始数据留服务器隔离区')

def cached_qc_result(job,records,resultdir):
    """QC completed on host may wait for Pi idle without re-decoding the videos."""
    proof=load(records/'qc-completed.json')
    if not proof:return None
    result=load(resultdir/'result.json')
    details=json.loads(job['details'])
    if (not result or hash_file(resultdir/'result.json')!=proof.get('result_sha256')
            or result.get('request_id')!=details.get('request_id')
            or result.get('native_sha256')!=details.get('native_sha256')):
        raise ValueError('Cached QC proof does not match the frozen capture')
    for relative,sha in result.get('artifacts',{}).items():
        path=Path(relative)
        target=resultdir/'artifacts'/path
        if path.is_absolute() or '..' in path.parts or target.is_symlink() or (resultdir/'artifacts').resolve() not in target.resolve().parents or hash_file(target)!=sha:
            raise ValueError('Cached QC artifact changed')
    return result

def ingest(job):
    details=json.loads(job['details']);did=job['device'];sid=job['session']
    records=DATA/'state/jobs'/did/sid;records.mkdir(parents=True,exist_ok=True)
    raw=DATA/'incoming'/did/sid
    raw_receipt=records/'original-verified.json'
    require_idle(did)
    if not raw_receipt.exists():
        update(job,state='copying',progress=0)
        rsync(job,details['path'],raw)
        update(job,state='verifying',progress=0)
        original=agent(did,'manifest',session_id=sid,timeout=1800)
        if original['native_sha256']!=details['native_sha256']:raise RuntimeError('Frozen capture identity changed')
        proof=verify_tree(raw,original,durable=True)
        atomic(records/'original-manifest.json',original);atomic(raw_receipt,proof)
        event(did,sid,'copy_verified','原始数据已完整复制并逐文件SHA256校验')
    else:original=load(records/'original-manifest.json')
    update(job,state='raw_ready',ingested=1,progress=100,transferred=original['total_bytes'])

def process(job):
    raise ValueError('Copy-only mode never runs QC or archive processing')
    details=json.loads(job['details']);did=job['device'];sid=job['session']
    records=DATA/'state/jobs'/did/sid;records.mkdir(parents=True,exist_ok=True)
    committed=load(records/'archive-receipt.json')
    if committed:
        finish_cleanup(job,records,committed)
        return
    raw=DATA/'incoming'/did/sid;work=DATA/'work'/did/sid
    if not (records/'original-verified.json').is_file():
        update(job,state='waiting',ingested=0,available=0,error='原始传输证明缺失，重新传输校验')
        return
    original=load(records/'original-manifest.json')
    if job['mode']=='archive' and not raw.exists():
        prior=DATA/'archive'/details.get('day',CONFIG['day'])/did/sid
        verify_tree(prior,original,durable=True);raw=prior
    result=None;applied=None
    if job['mode']=='qc':
        if not work.exists():local_copy(raw,work)
        output=DATA/'results'/did
        result=cached_qc_result(job,records,output/sid)
        if result is None:
            result=qc_run(job,work,output)
            atomic(records/'qc-completed.json',dict(result_sha256=hash_file(output/sid/'result.json'),completed_at=time.time()))
        update(job,level=result['level'])
        report=load(output/sid/'artifacts/qc_report.json',{});findings=[]
        for finding in report.get('findings',[]):
            if finding.get('level') in ('WARN','ERROR'):findings.append(dict(finding,modality=finding.get('subject','session')))
        for name,stream in report.get('streams',{}).items():
            for finding in stream.get('findings',[]):
                if finding.get('level') in ('WARN','ERROR'):findings.append(dict(finding,modality=name))
        details['qc_summary']=dict(error_count=sum(x['level']=='ERROR' for x in findings),warn_count=sum(x['level']=='WARN' for x in findings),findings=findings[:60])
        update(job,details=json.dumps(details,ensure_ascii=False))
        require_idle(did);update(job,state='applying',progress=0)
        resultdir=output/sid
        rsync(job,resultdir,'/home/orangepi/maintenance/server-qc/results/'+sid,push=True)
        applied=agent(did,'apply',session_id=sid,server_path=str(resultdir),timeout=1800)
        atomic(records/'pi-applied.json',applied)
        if applied.get('result_sha256')!=hash_file(resultdir/'result.json'):raise RuntimeError('Pi did not acknowledge the exact QC result')
        passed=result['level']!='ERROR'
    else:
        passed=True;update(job,level=details['level'])
    require_idle(did)
    final=load(records/'final-manifest.json')
    receipt=load(records/'archive-receipt.json')
    if not receipt:
        if job['mode']=='qc':
            update(job,state='final_sync',progress=0)
            rsync(job,details['path'],work,delete=True)
            final=agent(did,'manifest',session_id=sid,timeout=1800)
            proof=verify_tree(work,final,durable=True);source=work
        else:final=original;proof=verify_tree(raw,final,durable=True);source=raw
        category='archive' if passed else 'quarantine'
        destination=DATA/category/details.get('day',CONFIG['day'])/did/sid;destination.parent.mkdir(parents=True,exist_ok=True)
        update(job,state='archiving',progress=100)
        if destination.exists():
            verify_tree(destination,final,durable=True)
        else:source.rename(destination);sync_dir(destination.parent)
        atomic(records/'final-manifest.json',final)
        receipt=dict(version=1,device_id=did,session_id=sid,server_id=CONFIG['server_id'],archive_path=str(destination),archive_verified=True,
                     verification=proof,manifest=final,passed=passed,result_sha256=applied['result_sha256'] if applied else None,created_at=time.time())
        key=(ROOT/'secrets'/did).read_bytes();signature=hmac.new(key,json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode(),hashlib.sha256).hexdigest()
        receipt['signature']=signature;atomic(records/'archive-receipt.json',receipt)
        atomic(DATA/'receipts'/did/(sid+'.json'),receipt)
        update(job,archive_path=str(destination))
        event(did,sid,'archive_verified','服务器%s已完整落盘；结果已回传，准备安全清理Pi'%('合格归档' if passed else '不合格隔离副本'))
    finish_cleanup(job,records,receipt)

def worker(stage):
    while not STOP.is_set():
        with WORK_CHANGED:observed=WORK_VERSION
        job=claim(stage)
        if not job:
            with WORK_CHANGED:
                if WORK_VERSION==observed and not STOP.is_set():WORK_CHANGED.wait(timeout=2)
            continue
        try:
            require_storage(CONFIG)
            if shutil.disk_usage(DATA).free<30*1024**3:raise RuntimeError('SERVER_LOW_SPACE: keep Pi data; connect the archive drive')
            (ingest if stage=='upload' else process)(job)
        except Exception as exc:
            error=str(exc);delay=20 if 'PI_BUSY' in error else 60
            update(job,state='waiting',error=friendly_error(exc),available=time.time()+delay)
            event(job['device'],job['session'],'waiting',friendly_error(exc))
            print('DEBUG '+error,flush=True)

def network_metrics():
    with NET_LOCK:
        try:sample=(time.monotonic(),int((NET_COUNTERS/'rx_bytes').read_text()),int((NET_COUNTERS/'tx_bytes').read_text()))
        except OSError:return dict(ready=False,rx_MB_s=None,tx_MB_s=None)
        if NET_SAMPLES and (sample[1]<NET_SAMPLES[-1][1] or sample[2]<NET_SAMPLES[-1][2]):NET_SAMPLES.clear()
        NET_SAMPLES.append(sample)
        while len(NET_SAMPLES)>2 and sample[0]-NET_SAMPLES[0][0]>10:NET_SAMPLES.popleft()
        first=NET_SAMPLES[0];elapsed=sample[0]-first[0]
        if elapsed<1:return dict(ready=False,rx_MB_s=None,tx_MB_s=None)
        return dict(ready=True,rx_MB_s=(sample[1]-first[1])/elapsed/1e6,tx_MB_s=(sample[2]-first[2])/elapsed/1e6,sample_seconds=elapsed)

def standardized_summary():
    catalog=load(DATA/'standardized/_catalog.json')
    if catalog is None:return dict(ready=False,total=None,by_device={})
    if catalog.get('root')!=str(DATA/'standardized'):raise ValueError('Standard package catalog root mismatch')
    entries=catalog.get('entries',[]);counts=collections.Counter();seen=set()
    for entry in entries:
        relative=Path(entry['relative_path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:raise ValueError('Unsafe standard package path')
        name=relative.as_posix()
        if name in seen:raise ValueError('Duplicate standard package in catalog')
        seen.add(name);counts[relative.parts[0]]+=1
    if catalog.get('packages')!=len(seen):raise ValueError('Standard package catalog count mismatch')
    return dict(ready=True,total=len(seen),by_device=dict(counts),updated=catalog.get('created'))

def historical_recheck_jobs():
    paths=sorted((DATA/'qc-rechecks').glob('cp13-recheck-*/status.json'),reverse=True)
    if not paths:return [],[]
    value=load(paths[0],{})
    rows=value.get('jobs',[])
    if value.get('device')!='cp_0013' or any(r.get('mode')!='local_recheck' for r in rows):raise ValueError('Unexpected historical recheck status')
    return rows,[{k:value.get(k) for k in ['batch_id','device','state','total','completed','passed','failed','execution_errors']}]

def snapshot():
    with db() as c:
        jobs=[dict(x) for x in c.execute('SELECT * FROM jobs ORDER BY id DESC')]
        devices=[dict(x) for x in c.execute('SELECT * FROM devices ORDER BY id')]
        events=[dict(x) for x in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 80')]
    counts={}
    for row in jobs:
        counts[row['state']]=counts.get(row['state'],0)+1
        row['details']=json.loads(row['details'])
    historical,rechecks=historical_recheck_jobs()
    jobs+=historical
    for row in historical:counts[row['state']]=counts.get(row['state'],0)+1
    for device in devices:
        value=json.loads(device['details']);device['details']={k:value.get(k) for k in ['state','current_session','task_list']}
    disk=shutil.disk_usage(DATA)
    return dict(historical_rechecks=rechecks,standardized=standardized_summary(),historical_devices=[d['id'] for d in CONFIG['devices'] if d.get('historical_only')],continuous_ingestion=CONFIG.get('continuous_ingestion',False),day=CONFIG['day'],cutoff=CONFIG['cutoff'],server_id=CONFIG['server_id'],data_root=str(DATA),
                cleanup_enabled=load(ROOT/'config.json',CONFIG).get('cleanup_enabled',False),transfer_parallelism=TRANSFER_PARALLELISM,transfer_kib_s=TRANSFER_KIB_S,active_per_device=MAX_ACTIVE_PER_DEVICE,pipeline_workers=CONFIG.get('upload_workers',4)+CONFIG.get('processor_workers',2),upload_workers=CONFIG.get('upload_workers',4),processor_workers=CONFIG.get('processor_workers',2),counts=counts,jobs=jobs,devices=devices,events=events,
                qc_video_backend=CONFIG.get('qc_video_backend','cv2'),qc_workers=CONFIG['qc_workers'],network=network_metrics(),disk=dict(total=disk.total,used=disk.used,free=disk.free),now=time.time())

class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.connection.settimeout(15)
    def do_GET(self):
        try:
            if RECEIVER is not None:
                from receiver_http import get
                if get(self,RECEIVER):return
                if self.path=='/health':
                    from receiver_http import send
                    send(self,200,dict(ok=True,receive_strategy='manual_handoff_v1',storage=RECEIVER.snapshot()['storage']));return
                if self.path.split('?')[0] in ('/','/api/status'):
                    if self.path.split('?')[0]=='/api/status':
                        from receiver_http import send
                        try:value=snapshot();value['manual_receiver']=RECEIVER.snapshot()
                        except Exception:value=RECEIVER.snapshot()
                        send(self,200,value);return
                    data=(ROOT/'receiver.html').read_bytes();mime='text/html; charset=utf-8'
                    self.send_response(200);self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
                    self.end_headers();self.wfile.write(data);return
            if self.path.split('?')[0]=='/api/status':data=json.dumps(snapshot(),ensure_ascii=False,allow_nan=False).encode();mime='application/json; charset=utf-8'
            elif self.path=='/health':require_storage(CONFIG);data=b'{"ok":true}';mime='application/json'
            elif self.path.split('?')[0]=='/':data=(ROOT/'index.html').read_bytes();mime='text/html; charset=utf-8'
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        except Exception as exc:self.send_error(500,str(exc))
    def do_POST(self):
        try:
            if RECEIVER is not None:
                from receiver_http import post
                if post(self,RECEIVER):return
            self.send_error(404)
        except Exception:
            from receiver_http import send
            send(self,503,dict(error='control_unavailable',message='控制账本暂不可用，未执行新的删除授权'))
    def log_message(self,*args):pass

def main():
    global RECEIVER
    import faulthandler,signal
    faulthandler.register(signal.SIGUSR1,all_threads=True)
    if (ROOT/'state/STORAGE_FAULT_HOLD').exists():
        # Keep the console visible, but use an independent read-only handler while
        # the persistent fault hold exists. This branch never starts the receiver,
        # scanner, workers, or any external-storage access.
        sys.path.insert(0,'/home/l2-headring-qc/Documents/Codex/2026-11-09/ianz/work/recovery-service')
        from fault_dashboard import serve
        serve(CONFIG.get('port',8788));return
    if CONFIG.get('receive_strategy')!='manual_handoff_v1':raise RuntimeError('Copy-only mode requires manual handoff strategy')
    if CONFIG.get('receive_strategy')=='manual_handoff_v1':
        from handoff_receiver import HandoffReceiver
        RECEIVER=HandoffReceiver(sys.modules[__name__]);RECEIVER.start()
    else:
        initialize()
        threading.Thread(target=scanner,daemon=True).start()
        for _ in range(CONFIG.get('upload_workers',4)):threading.Thread(target=worker,args=('upload',),daemon=True).start()
        for _ in range(CONFIG.get('processor_workers',2)):threading.Thread(target=worker,args=('process',),daemon=True).start()
    server=http.server.ThreadingHTTPServer(('0.0.0.0',CONFIG.get('port',8788)),Handler)
    try:server.serve_forever()
    finally:STOP.set();wake_workers();server.server_close()

if __name__=='__main__':main()
