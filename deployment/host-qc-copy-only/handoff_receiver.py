"""Manual raw custody queue. PC processing does not require Pi raw directories."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import signal
import subprocess
import threading
import time
import uuid

from receiver_state import ReceiverStore,TARGETS
from raw_handoff import canonical,signed,sha_value,validate_manifest,verify_signature,same_file_bytes,RAW_PURPOSE,RESULT_PURPOSE

COPY_PURPOSE='host_raw_copy_only_v1'

class Paused(RuntimeError):pass

class HandoffReceiver:
    def __init__(self,legacy):
        self.legacy=legacy;self.root=legacy.ROOT;self.config=legacy.CONFIG;self.data=legacy.DATA
        self.store=ReceiverStore(Path(self.config.get('receiver_state_root',str(self.root/'state/receiver'))))
        self.stop=legacy.STOP;self.wake=threading.Event();self.qc_lock=threading.Lock()
        self.pi_locks={d:threading.RLock() for d in TARGETS}
        self.devices={d['id']:d for d in self.config['devices']
                      if d['id'] in TARGETS and d.get('enabled',True) is not False}

    def _storage(self):self.legacy.require_storage(self.config)

    def _guard(self,revision=None):
        state=self.store.control()
        if not state['receiving'] or revision is not None and state['revision']!=revision:
            raise Paused('Receiver stopped; Pi originals and partial PC transfer retained')

    @staticmethod
    def _end_process(process):
        if process.poll() is not None:return
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)

    def rpc(self,device,action,allow_committed=False,**values):
        if action not in ('status','scan','manifest'):raise Paused('Copy-only mode forbids mutating Pi actions')
        revision=self.store.control()['revision']
        if not allow_committed:self._guard(revision)
        command=self.legacy.ssh(device,self.legacy.AGENT)
        request=canonical(dict(action=action,device_id=device,**values))
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,start_new_session=True)
        began=time.monotonic();first=True
        try:
            while True:
                if self.stop.is_set():raise Paused('Service shutting down; transaction will recover')
                if not allow_committed:self._guard(revision)
                if time.monotonic()-began>1200:raise TimeoutError('Pi RPC timeout')
                try:
                    out,err=process.communicate(input=request if first else None,timeout=.25)
                    break
                except subprocess.TimeoutExpired:first=False
            if process.returncode:raise RuntimeError(err.decode(errors='replace')[-1800:] or 'Pi agent failed')
            result=json.loads(out)
            if result.get('device_id') not in (None,device):raise ValueError('Unexpected Pi agent identity')
            return result
        finally:self._end_process(process)

    def _idle(self,device,allow_committed=False):
        state=self.rpc(device,'status',allow_committed=allow_committed)
        if (state.get('state') not in ('device_confirm','idle','exhausted','failed_wait')
                or state.get('session') or (state.get('worker') or {}).get('recording_active')):
            raise Paused('Pi is preparing/recording/saving; originals retained')

    def _manifest(self,path):
        rows=[]
        if path.is_symlink() or not path.is_dir():raise ValueError('Invalid host custody directory')
        for parent,dirs,names in os.walk(path,followlinks=False):
            if any(Path(parent,name).is_symlink() for name in dirs):raise ValueError('Linked custody directory')
            for name in names:
                p=Path(parent,name)
                if p.is_symlink() or not p.is_file():raise ValueError('Nonregular custody file')
                info=p.stat();digest=hashlib.sha256()
                with p.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
                after=p.stat()
                if (info.st_size,info.st_mtime_ns,info.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                    raise ValueError('Host file changed while hashing')
                rows.append(dict(path=p.relative_to(path).as_posix(),bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns,inode=info.st_ino,sha256=digest.hexdigest()))
        rows.sort(key=lambda row:row['path'])
        value=dict(files=rows,total_files=len(rows),total_bytes=sum(r['bytes'] for r in rows),digest=sha_value(rows),
            native_sha256=self.legacy.hash_file(path/'native_capture_summary.json'))
        validate_manifest(value);return value

    def _scan_one(self,definition):
        device=definition['id']
        try:
            if not self.store.control()['receiving']:
                # Status-only inventory never walks source files when stopped.
                state=self.legacy.agent(device,'status',timeout=15)
                self.store.device(device,online=True,state=state.get('state'),active_session=state.get('session'))
                return
            with self.pi_locks[device]:
                result=self.rpc(device,'scan',cutoff=time.time(),day='*',manual_handoff=True)
            if result.get('device_id')!=device:raise ValueError('Pi identity mismatch')
            self.store.device(device,online=True,state=result.get('state'),active_session=result.get('current_session'))
            for item in result.get('items',[]):
                only=self.config.get('accept_only_sessions')
                if only is not None and item.get('session_id') not in only:continue
                if item.get('day','')<self.config.get('minimum_capture_date','2026-11-01'):continue
                if item.get('stop_time',0)<definition.get('minimum_stop_time',0):continue
                # Already archived old protocol jobs are not silently migrated/re-uploaded.
                receipt=self.data/'receipts'/device/(item['session_id']+'.json')
                if receipt.is_file():continue
                item=dict(item);item['request_id']=item.get('request_id') or uuid.uuid4().hex
                self.store.enqueue(device,item)
            self.wake.set()
        except Exception as error:self.store.device(device,online=False,error=str(error)[-1000:])

    def scanner(self):
        while not self.stop.is_set():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(self._scan_one,self.devices.values()))
            except Exception as error:print('MANUAL_SCAN_ERROR '+str(error)[-400:],flush=True)
            self.wake.wait(15);self.wake.clear()

    def _transfer(self,job,target,push=False):
        if push:raise Paused('Copy-only mode forbids Pi writes')
        revision=self.store.control()['revision'];self._guard(revision);self._storage();self._idle(job['device'])
        target.parent.mkdir(parents=True,exist_ok=True)
        source=job['details']['path'] if not push else str(target)
        destination=str(target) if not push else '/home/orangepi/maintenance/server-qc/results/'+job['session']
        command=['rsync','-a','--partial','--partial-dir=.rsync-partial','--protect-args','--stats',
                 '--no-inc-recursive','-e','ssh -q -F '+str(self.root/'ssh/config')]
        limit=int(self.config.get('transfer_kib_s',0))
        if limit:command.append('--bwlimit='+str(limit))
        if push:command.extend([source+'/',job['device']+':'+destination+'/'])
        else:command.extend([job['device']+':'+source+'/',destination+'/'])
        log=self.data/'logs'/('handoff-'+job['device']+'-'+job['session']+('-reply.log' if push else '-receive.log'))
        log.parent.mkdir(parents=True,exist_ok=True)
        began=time.monotonic();last_idle=began
        with log.open('a') as stream:
            process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,
                                     env=dict(os.environ,LC_ALL='C'))
            try:
                while process.poll() is None:
                    if self.stop.wait(.25):raise Paused('Service stopped during transfer')
                    self._guard(revision)
                    if time.monotonic()-last_idle>2:self._idle(job['device']);last_idle=time.monotonic()
                if process.returncode:raise RuntimeError(log.read_text(errors='replace')[-1200:])
            finally:self._end_process(process)
        if not push:self.store.update(job['id'],transfer_seconds=time.monotonic()-began)

    def _durable_original_parents(self,parent):
        """Create and sync each new archive ancestor under data/originals."""
        root=self.data/'originals';parent=Path(parent)
        if root.is_symlink() or not root.is_dir() or root.resolve()!=root:raise ValueError('Unsafe originals root')
        relative=parent.relative_to(root);cursor=root
        for part in relative.parts:
            child=cursor/part
            if child.is_symlink():raise ValueError('Linked originals ancestor')
            if not child.exists():
                child.mkdir();self.legacy.sync_dir(cursor);self.legacy.sync_dir(child)
            if not child.is_dir():raise ValueError('Invalid originals ancestor')
            cursor=child
        if cursor.resolve()!=cursor:raise ValueError('Linked originals ancestor')

    def receive(self,job):
        identifier=job['id'];device=job['device'];sid=job['session'];day=job['details']['day']
        if not re.fullmatch(r'20\d\d-\d\d-\d\d',day):raise ValueError('Invalid captured day')
        raw=self.data/'originals'/device/'received'/day/sid
        incoming=self.data/'incoming/manual-handoff'/device/sid
        with self.pi_locks[device]:
            revision=self.store.control()['revision'];self._guard(revision);self._storage();self._idle(device)
            if job.get('raw_receipt') or job.get('release_decision_at'):
                raise Paused('Existing handoff transaction retained for review')
            if job.get('copy_receipt'):
                proof=verify_signature(job['copy_receipt'],(self.root/'secrets'/device).read_bytes(),COPY_PURPOSE)
                manifest=validate_manifest(proof.get('manifest'))
                source=Path(job['details']['path'])
                if (sha_value(job['copy_receipt'])!=job.get('copy_receipt_sha256') or proof.get('device_id')!=device
                    or proof.get('session_id')!=sid or proof.get('host_path')!=str(raw)
                    or proof.get('server_id')!=self.config['server_id'] or proof.get('host_storage_uuid')!=self.config['storage_uuid']
                    or proof.get('qc_request_id')!=job['details']['request_id']
                    or not source.is_absolute() or source.name!=sid or str(source)!=job['details']['path']
                    or manifest.get('device_id')!=device or manifest.get('session_id')!=sid
                    or manifest.get('root')!=str(source.parent) or manifest.get('native_sha256')!=job['details']['native_sha256']):
                    raise ValueError('Copy-only receipt identity differs')
                self.legacy.verify_tree(raw,manifest,durable=True)
                self.store.commit_copy(identifier,revision,str(raw),job['copy_receipt'],job['copy_receipt_sha256']);return
            manifest=job.get('manifest')
            if manifest is None:
                manifest=self.rpc(device,'manifest',session_id=sid);validate_manifest(manifest)
                if manifest['native_sha256']!=job['details']['native_sha256']:raise ValueError('Frozen native identity changed')
                self.store.update(identifier,manifest=manifest)
            validate_manifest(manifest)
            source=Path(job['details']['path'])
            if (not source.is_absolute() or str(source)!=job['details']['path'] or source.name!=sid
                    or manifest.get('device_id')!=device or manifest.get('session_id')!=sid
                    or manifest.get('root')!=str(source.parent) or manifest.get('native_sha256')!=job['details']['native_sha256']):
                raise ValueError('Copy manifest does not match source device/session')
            if not raw.exists():
                minimum=self.config.get('copy_only_min_free_bytes',10*1024**3)
                if type(minimum) is not int or minimum<0:raise ValueError('Invalid copy-only free-space reserve')
                if shutil.disk_usage(self.data).free < manifest['total_bytes']+minimum:
                    raise Paused('Insufficient disk space to retain a full copy')
                self._transfer(job,incoming)
                fresh=self.rpc(device,'manifest',session_id=sid)
                if (fresh.get('device_id')!=device or fresh.get('session_id')!=sid or fresh.get('root')!=str(source.parent) or not same_file_bytes(fresh,manifest)):raise ValueError('Source changed during receive; refusing handoff')
                self.legacy.verify_tree(incoming,manifest,durable=True)
                self._durable_original_parents(raw.parent)
                if raw.parent.resolve()!=raw.parent or raw.is_symlink():raise ValueError('Custody destination linked')
                incoming.rename(raw);self.legacy.sync_dir(incoming.parent);self.legacy.sync_dir(raw.parent)
            self.legacy.verify_tree(raw,manifest,durable=True)
            fresh=self.rpc(device,'manifest',session_id=sid)
            if (fresh.get('device_id')!=device or fresh.get('session_id')!=sid or fresh.get('root')!=str(source.parent) or not same_file_bytes(fresh,manifest)):raise ValueError('Source changed before copy commit')
            receipt=signed(dict(version=1,purpose=COPY_PURPOSE,device_id=device,session_id=sid,
                server_id=self.config['server_id'],transaction_id=uuid.uuid4().hex,manifest=manifest,
                host_path=str(raw),host_storage_uuid=self.config['storage_uuid'],host_verified=True,
                host_fsynced=True,qc_request_id=job['details']['request_id'],committed_at=time.time()),
                (self.root/'secrets'/device).read_bytes())
            self.store.commit_copy(identifier,revision,str(raw),receipt,sha_value(receipt))
            self.wake.set()

    def release(self,job):
        raise Paused('Copy-only mode never releases, processes, or replies')
        with self.pi_locks[job['device']]:
            already_authorized=bool(job.get('release_decision_at'));revision=self.store.control()['revision']
            if not already_authorized:self._guard(revision)
            self._storage();self._idle(job['device'],allow_committed=already_authorized)
            self.legacy.verify_tree(Path(job['raw_path']),job['raw_receipt']['manifest'],durable=True)
            if not already_authorized:self.store.authorize_release(job['id'],revision)
            status=self.rpc(job['device'],'handoff_status',allow_committed=True,session_id=job['session'],
                handoff_receipt_sha256=job['raw_receipt_sha256'])
            if status.get('state')=='deleted':
                if status.get('receipt_sha256')!=job['raw_receipt_sha256']:raise ValueError('Handoff query ACK mismatch')
                self.store.update(job['id'],release_state='deleted',release_ack=status,release_error=None)
                self.wake.set();return
            # A persisted exact decision is allowed to finish after STOP; no
            # new source transaction is authorized once STOP wins the gate.
            ack=self.rpc(job['device'],'raw_handoff',allow_committed=True,receipt=job['raw_receipt'])
            if ack.get('state')!='deleted' or ack.get('receipt_sha256')!=job['raw_receipt_sha256']:
                raise ValueError('Pi did not acknowledge the exact handoff')
            self.store.update(job['id'],release_state='deleted',release_ack=ack,release_error=None)
            self.wake.set()

    def _qc(self,job,work):
        output=self.data/'results'/job['device'];output.mkdir(parents=True,exist_ok=True)
        resultdir=output/job['session'];result_path=resultdir/'result.json'
        if result_path.is_file():
            result=json.loads(result_path.read_text());self._validate_result(job,resultdir,result);return result
        log=self.data/'logs'/('handoff-'+job['device']+'-'+job['session']+'-qc.log')
        command=[str(self.root/'.venv/bin/python'),str(self.root/'collector/orangepi/src/orangepi_collect/framework/qc_host.py'),
                 'run',str(work),'--results',str(output),'--workers',str(self.config.get('qc_workers',6))]
        with self.qc_lock,log.open('w') as stream:
            process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,
                env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',ORANGEPI_HOST_QC='1',
                         ORANGEPI_HOST_VIDEO_BACKEND=self.config.get('qc_video_backend','nvdec')))
            try:
                while process.poll() is None:
                    if self.stop.wait(.3):raise Paused('QC service shutdown; PC originals retained')
                if process.returncode:raise RuntimeError(log.read_text(errors='replace')[-1800:])
            finally:self._end_process(process)
        result=json.loads(result_path.read_text());self._validate_result(job,resultdir,result);return result

    def _validate_result(self,job,resultdir,result):
        if resultdir.is_symlink() or resultdir.resolve()!=resultdir:raise ValueError('QC result root linked')
        if (result.get('session_id')!=job['session'] or result.get('request_id')!=job['details']['request_id']
                or result.get('native_sha256')!=job['details']['native_sha256'] or result.get('level') not in ('INFO','WARN','ERROR')):
            raise ValueError('QC result differs from committed raw identity')
        if 'qc_report.json' not in result.get('artifacts',{}):raise ValueError('QC report missing')
        for relative,sha in result['artifacts'].items():
            path=Path(relative);artifact=resultdir/'artifacts'/path
            if path.is_absolute() or '..' in path.parts or artifact.is_symlink() or (resultdir/'artifacts').resolve() not in artifact.resolve().parents:
                raise ValueError('Unsafe QC artifact')
            if self.legacy.hash_file(artifact)!=sha:raise ValueError('QC artifact checksum changed')

    def process(self,job):
        raise Paused('Copy-only mode never releases, processes, or replies')
        self._storage();raw=Path(job['raw_path']);sid=job['session'];device=job['device'];identifier=job['id']
        self.legacy.verify_tree(raw,job['raw_receipt']['manifest'],durable=True)
        receipt_path=self.data/'receipts'/device/(sid+'.json')
        if receipt_path.is_file():
            committed=json.loads(receipt_path.read_text())
            verify_signature(committed,(self.root/'secrets'/device).read_bytes(),'host_qc_archive_v1')
            if committed.get('raw_handoff_receipt_sha256')!=job['raw_receipt_sha256']:
                raise ValueError('Existing archive belongs to another custody transaction')
            expected=self.data/('archive' if committed['passed'] else 'quarantine')/job['details']['day']/device/sid
            if committed.get('archive_path')!=str(expected):raise ValueError('Committed archive escaped job destination')
            self.legacy.verify_tree(Path(committed['archive_path']),committed['manifest'],durable=True)
            resultdir=self.data/'results'/device/sid;result=json.loads((resultdir/'result.json').read_text())
            self._validate_result(job,resultdir,result)
            if self.legacy.hash_file(resultdir/'result.json')!=committed['result_sha256']:raise ValueError('Committed result changed')
            self.store.update(identifier,process_state='complete',archive_path=committed['archive_path'],
                archive_receipt=committed,result=result,result_sha256=committed['result_sha256'],level=result['level'],
                raw_level=result.get('raw_level'),reply_state='queued',process_error=None)
            self.wake.set();return
        parent=self.data/'work/manual-handoff'/job['raw_receipt']['transaction_id'];work=parent/sid
        parent.mkdir(parents=True,exist_ok=True)
        if not work.exists():
            temporary=parent/(sid+'.copy');temporary.mkdir(exist_ok=True)
            subprocess.run(['cp','-a','--reflink=auto',str(raw)+'/.',str(temporary)+'/'],check=True,capture_output=True,timeout=1800)
            self.legacy.verify_tree(temporary,job['raw_receipt']['manifest'],durable=True)
            temporary.rename(work);self.legacy.sync_dir(parent)
        pending=dict(version=1,request_id=job['details']['request_id'],session_id=sid,
            native_sha256=job['details']['native_sha256'],state='pending',created_at=job['raw_receipt']['committed_at'])
        self.legacy.atomic(work/'qc_pending.json',pending)
        applied=work/'qc_applied.json'
        if applied.exists():
            history=work/'qc_history';history.mkdir(exist_ok=True);applied.rename(history/'pi-qc-applied.json')
        result=self._qc(job,work);resultdir=self.data/'results'/device/sid
        # Rehydrate checked artifacts on a restart/cache hit; a fresh work copy
        # must never be archived as if cached QC had transformed it already.
        for relative,sha in result['artifacts'].items():
            source=resultdir/'artifacts'/relative;target=work/relative;target.parent.mkdir(parents=True,exist_ok=True)
            if target.is_symlink():raise ValueError('Linked private work result destination')
            if not target.is_file() or self.legacy.hash_file(target)!=sha:
                temp=target.with_name(target.name+'.import');shutil.copy2(source,temp)
                with temp.open('rb') as f:os.fsync(f.fileno())
                temp.replace(target)
        self.store.update(identifier,result=result,result_sha256=self.legacy.hash_file(resultdir/'result.json'),
                          level=result['level'],raw_level=result.get('raw_level'))
        summary=json.loads((work/'native_capture_summary.json').read_text());outcome='qc_error' if result['level']=='ERROR' else 'success'
        summary.update(status=outcome,qc_state='completed',qc_level=result['level'],qc_raw_level=result.get('raw_level'),
                       qc_executor='linux_host',qc_elapsed_seconds=result['elapsed_seconds'])
        self.legacy.atomic(work/'capture_summary.json',summary)
        import yaml
        meta=yaml.safe_load((work/'meta.yaml').read_text()) or {};meta.update(result.get('metadata',{}))
        meta.update(status=outcome,qc_level=result['level'],qc_state='completed',retention_state='host_custody',
                    upload_state='pending_cloud',pc_retention_rule='72_hours_after_verified_cloud_confirmation')
        (work/'meta.yaml').write_text(yaml.safe_dump(meta,allow_unicode=True,sort_keys=False))
        self.legacy.atomic(work/'status.json',dict(state=outcome,recording_active=False,qc_state='completed',qc_level=result['level']))
        manifest=self._manifest(work);manifest.update(device_id=device,session_id=sid)
        category='quarantine' if outcome=='qc_error' else 'archive';destination=self.data/category/job['details']['day']/device/sid
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():self.legacy.verify_tree(destination,manifest,durable=True)
        else:work.rename(destination);self.legacy.sync_dir(destination.parent)
        verification=self.legacy.verify_tree(destination,manifest,durable=True)
        receipt=signed(dict(version=1,purpose='host_qc_archive_v1',device_id=device,session_id=sid,
            server_id=self.config['server_id'],archive_path=str(destination),archive_verified=True,
            verification=verification,manifest=manifest,passed=result['level']!='ERROR',
            raw_handoff_receipt_sha256=job['raw_receipt_sha256'],result_sha256=self.legacy.hash_file(resultdir/'result.json'),
            created_at=time.time()),(self.root/'secrets'/device).read_bytes())
        self.legacy.atomic(self.data/'receipts'/device/(sid+'.json'),receipt)
        self.store.update(identifier,process_state='complete',archive_path=str(destination),archive_receipt=receipt,
            reply_state='queued',standard_state='waiting' if outcome=='success' else 'excluded_qc_failed',process_error=None)
        self.wake.set()

    def reply(self,job):
        raise Paused('Copy-only mode never releases, processes, or replies')
        with self.pi_locks[job['device']]:
            self._guard();self._idle(job['device']);resultdir=self.data/'results'/job['device']/job['session']
            ready=self.rpc(job['device'],'prepare_handoff_results',session_id=job['session'],
                           handoff_receipt_sha256=job['raw_receipt_sha256'])
            expected='/home/orangepi/maintenance/server-qc/results/'+job['session']
            if ready.get('result_directory')!=expected or ready.get('ready') is not True:raise ValueError('Unexpected result destination')
            self._transfer(job,resultdir,push=True)
            receipt=signed(dict(version=1,purpose=RESULT_PURPOSE,device_id=job['device'],session_id=job['session'],
                server_id=self.config['server_id'],handoff_receipt_sha256=job['raw_receipt_sha256'],
                result_sha256=job['result_sha256'],server_path=str(resultdir)),(self.root/'secrets'/job['device']).read_bytes())
            ack=self.rpc(job['device'],'apply_handoff_result',receipt=receipt)
            if ack.get('result_sha256')!=job['result_sha256']:raise ValueError('Post-handoff result ACK mismatch')
            self.store.update(job['id'],reply_state='complete',reply_ack=ack,reply_error=None)

    def worker(self,stage):
        method=getattr(self,stage)
        while not self.stop.is_set():
            job=None
            try:
                job=self.store.claim(stage)
                if job is None:self.wake.wait(1);self.wake.clear();continue
                method(job)
            except Exception as error:
                if job:
                    field={'receive':'receive_state','release':'release_state','process':'process_state','reply':'reply_state'}[stage]
                    try:self.store.update(job['id'],**{field:'copy_only_paused' if isinstance(error,Paused) else 'copy_only_retry',
                        stage+'_error':str(error)[-1800:],stage+'_available':time.time()+min(300,2**min(job.get(stage+'_attempts',1),8))})
                    except Exception as ledger_error:print('MANUAL_LEDGER_ERROR '+str(ledger_error)[-400:],flush=True)
                else:print('MANUAL_WORKER_ERROR '+str(error)[-400:],flush=True)
                self.stop.wait(1)

    def output_watcher(self):
        while not self.stop.is_set():
            try:
                self._storage()
                from pc_retention import Retention
                retention=Retention(self.root/'state/pc-retention')
                standard_state=Path(self.config.get('standardizer_state_root',str(self.root.parent/'headring-standardizer/state')))
                cloud=Path(self.config.get('cloud_upload_state_root',str(self.root/'integrations/robot-uploader-1.0.0/adapter/cloud-jobs')))/'cloud-upload.sqlite3'
                for job in self.store.jobs():
                    if job.get('process_state')!='complete':continue
                    path=standard_state/'jobs'/job['device']/(job['session']+'.json')
                    if not path.is_file():continue
                    status=json.loads(path.read_text());self.store.update(job['id'],standard_state=status.get('state'),standard_error=status.get('reason'))
                    if status.get('state')!='complete' or not cloud.is_file():continue
                    output=Path(status['output']);relative=output.relative_to(self.data/'standardized')
                    info=json.loads((output/'meta/info.json').read_text());profile=info.get('pi_export',{}).get('profile')
                    if profile not in ('L2','L3'):continue
                    if info.get('pi_export',{}).get('source_digest')!=job.get('archive_receipt',{}).get('manifest',{}).get('digest'):
                        raise ValueError('Standard package does not bind this raw/QC archive')
                    target=('EGOL2' if profile=='L2' else 'EGOL3_hand')+'/'+relative.as_posix()
                    connection=sqlite3.connect('file:'+str(cloud)+'?mode=ro',uri=True)
                    row=connection.execute('SELECT state,receipt FROM uploads WHERE target=?',(target,)).fetchone();connection.close()
                    if row is None:continue
                    if row[0]!='remote_confirmed':self.store.update(job['id'],cloud_state=row[0]);continue
                    receipt=json.loads(row[1]);retention.verify(receipt)
                    if receipt.get('source_path')!=str(output) or receipt.get('dataset')!=target:raise ValueError('Cloud lineage differs from standard output')
                    self.store.update(job['id'],cloud_state='remote_confirmed',cloud_confirmed_at=receipt['cloud_confirmed_at'],
                        pc_delete_after=receipt['pc_delete_after'],retention_hold=False,cloud_dataset=target)
            except Exception as error:print('OUTPUT_WATCH_HOLD '+str(error)[-500:],flush=True)
            self.stop.wait(15)

    def start(self):
        # No release, QC, reply, output watcher or retention worker is started.
        for job in self.store.jobs():
            if job.get('release_decision_at') or job.get('raw_receipt'):
                raise Paused('Existing handoff transaction requires review before copy-only start')
        threading.Thread(target=self.scanner,name='manual-scanner',daemon=True).start()
        threading.Thread(target=self.worker,args=('receive',),name='manual-receive',daemon=True).start()

    def snapshot(self):
        jobs=self.store.jobs();state=self.store.control()
        stopping=not state['receiving'] and any(j.get('receive_state')=='copy_only_running' for j in jobs)
        try:
            self._storage();disk=shutil.disk_usage(self.data);storage=dict(ready=True,free=disk.free,total=disk.total)
        except Exception as error:storage=dict(ready=False,error=str(error)[-500:])
        fields=['id','device','session','receive_state','release_state','process_state','reply_state','standard_state',
            'level','raw_level','receive_error','release_error','process_error','reply_error','transfer_seconds',
            'raw_path','archive_path','cloud_state','cloud_confirmed_at','pc_delete_after','retention_hold']
        visible=[]
        for job in jobs:
            row={key:job.get(key) for key in fields}
            row['reference_name']=job.get('details',{}).get('reference_name','')
            row['bytes']=job.get('details',{}).get('bytes',0);visible.append(row)
        return dict(copy_only_version='20261119-copy-only-v1',copy_only=True,
            control=dict(state,stopping=stopping),jobs=visible,devices=self.store.devices(),storage=storage,
            data_root=str(self.data),retention=dict(seconds=259200,starts_at='verified_cloud_confirmation',
            delete_before_cloud=False),cloud_scope=['EGOL2','EGOL3_hand'],
            test_session_filter=self.config.get('accept_only_sessions'),now=time.time())
