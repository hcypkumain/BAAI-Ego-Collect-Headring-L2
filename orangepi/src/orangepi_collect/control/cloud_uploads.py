"""Phone-controlled persistent queue. Transfer never runs in the capture loop."""
import copy,datetime,hashlib,json,os,queue,re,select,subprocess,threading,time,uuid
from pathlib import Path
from cryptography.fernet import Fernet
from cloud_common import CloudError,atomic,read_json,source_manifest,same_manifest

DEFAULTS=dict(project_name='EGOL2',target_parent='EGOL2/pi-collect',task_id=None)
ACTIVE={'checking','uploading','verifying','cleaning'}
CST=datetime.timezone(datetime.timedelta(hours=8))

def settings(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULTS):raise CloudError('上传目标设置无效')
    project=value['project_name'];parent=value['target_parent'];task=value['task_id']
    if not isinstance(project,str) or not 1<=len(project.strip())<=128:raise CloudError('请输入平台项目名称')
    if not isinstance(parent,str) or not 1<=len(parent)<=200 or any(part in ('','.','..') for part in parent.split('/')) or re.search(r'[\\\x00-\x20?%#]',parent):
        raise CloudError('目标路径无效，请使用项目名/目录名')
    if task is not None and (type(task) is not int or task<=0):raise CloudError('平台任务 ID 必须为正整数或留空')
    return dict(project_name=project.strip(),target_parent=parent,task_id=task)

class UploadManager:
    def __init__(self,base,store,display_state,autostart=True):
        self.base=Path(base);self.store=store;self.display_state=display_state
        self.root=self.base/'runtime/cloud_uploads';self.root.mkdir(parents=True,exist_ok=True);self.root.chmod(0o700)
        self.lock=threading.RLock();self.stop=threading.Event();self.active=None;self.process=None;self.lines=queue.Queue();self.error=None
        self.cleanup_cache={}
        self.state=read_json(self.root/'queue.json',dict(version=1,jobs=[],paused=False,settings=dict(DEFAULTS)))
        if not isinstance(self.state,dict) or self.state.get('version')!=1 or not isinstance(self.state.get('jobs'),list):raise CloudError('上传队列文件需要修复，未改动原始数据')
        for job in self.state['jobs']:
            receipt=read_json(self.root/'jobs'/job['id']/'receipt.json')
            if receipt and receipt.get('platform_completion_acknowledged') and not job.get('cleanup_requested'):
                job.update(state='success',phase='已上传并核验，可清理本地数据',completed_at=receipt['completed_at'])
            if job['state'] in ACTIVE:
                job.update(state='interrupted',phase='上次任务被中断，可重试核验',error='采集服务曾重启，保留已有云端对象与本地文件')
        self._save()
        key=self.root/'vault.key'
        if not key.exists():
            fd=os.open(str(key),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(Fernet.generate_key());stream.flush();os.fsync(stream.fileno())
        self.fernet=Fernet(key.read_bytes());self.thread=None
        if autostart:self.thread=threading.Thread(target=self._run,name='cloud-queue',daemon=True);self.thread.start()

    def _save(self):atomic(self.root/'queue.json',self.state)
    def _job(self,identifier):
        job=next((job for job in self.state['jobs'] if job['id']==identifier),None)
        if job is None:raise CloudError('找不到上传任务')
        return job
    def credentials(self):
        path=self.root/'credentials.enc'
        if not path.exists():return None
        return json.loads(self.fernet.decrypt(path.read_bytes()).decode())
    def credential_status(self):
        value=self.credentials()
        return dict(configured=value is not None,masked_ak=('••••'+value['ak'][-4:]) if value else '')
    def set_credentials(self,value):
        if not isinstance(value,dict) or set(value)!={'ak','sk'} or any(not isinstance(value[k],str) or not 1<=len(value[k].strip())<=512 for k in ('ak','sk')):
            raise CloudError('请输入有效 AK 和 SK')
        with self.lock:
            if self.active:raise CloudError('请先暂停当前上传，再更换凭据')
            raw=self.fernet.encrypt(json.dumps({k:value[k].strip() for k in ('ak','sk')}).encode())
            path=self.root/'credentials.enc';temp=path.with_suffix('.tmp')
            fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(str(temp),str(path))
            from cloud_common import sync_dir
            sync_dir(self.root)
        return self.credential_status()
    def clear_credentials(self):
        with self.lock:
            if self.active:raise CloudError('请先暂停上传，再清除凭据')
            path=self.root/'credentials.enc'
            if path.exists():path.unlink()
            self.state['paused']=True;self._save()
        return dict(configured=False)
    def collection_busy(self):
        value=self.display_state();privacy=value.get('privacy') or {};worker=value.get('worker') or {}
        return value.get('state') not in ('device_confirm','idle','failed_wait','exhausted') or privacy.get('required') is not False or worker.get('recording_active') is True

    def enqueue(self,value):
        if not isinstance(value,dict) or set(value)!={'selection','settings'}:raise CloudError('上传请求无效')
        selected=value['selection'];config=settings(value['settings'])
        if not isinstance(selected,list) or not 1<=len(selected)<=200:raise CloudError('每次请选择1到200段已保留采集')
        if self.credentials() is None:raise CloudError('请先保存平台 AK/SK')
        ids=[]
        with self.lock:
            root,volume=self.store.roots()
            for entry in selected:
                if not isinstance(entry,dict) or set(entry)!={'session_id','token'}:raise CloudError('批次选择无效')
                path=self.store.path(entry['session_id']);meta=self.store.metadata(path)
                if meta['token']!=entry['token'] or not meta['retained']:raise CloudError('批次状态已变化或尚未确认保留，请刷新列表')
                if meta.get('qc_state')=='pending':raise CloudError('该段等待主机质检，尚不能作为已检数据上传')
                old=next((job for job in reversed(self.state['jobs']) if job['session_id']==meta['id'] and not job.get('local_deleted')),None)
                if old and old['state'] in ACTIVE|{'queued','paused','cleanup_queued','success'}:raise CloudError('所选批次已在队列或已上传，请刷新；失败任务请用重试')
                ids.append((path,meta))
            if len({meta['id'] for _,meta in ids})!=len(ids):raise CloudError('批次不能重复选择')
            daily=getattr(self,'daily_collection',None)
            reports={}
            if daily is not None:
                for day in sorted({datetime.datetime.fromtimestamp(meta['created_unix'],CST).date().isoformat() for _,meta in ids}):
                    report,_=daily.export(day);reports[day]=str(report)
            result=[]
            for path,meta in ids:
                meta=dict(meta,day=datetime.datetime.fromtimestamp(meta['created_unix'],CST).date().isoformat())
                from project_paths import COLLECTION_MODE
                identifier=uuid.uuid4().hex;remote=config['target_parent']+'/'+COLLECTION_MODE+'/'+meta['day']+'/'+meta['id']+'-'+identifier[:8]
                snapshot={k:meta.get(k) for k in ('id','token','reference_name','qc_level','day','duration','retained','reference_state')}
                job=dict(collection_mode=COLLECTION_MODE,id=identifier,session_id=meta['id'],source=str(path),allowed_root=str(root),volume=str(volume),source_token=meta['token'],
                    daily_report=reports.get(meta['day']),recording=snapshot,settings=dict(config),remote_path=remote,state='queued',phase='等待上传',created_at=time.time(),
                    total_files=0,total_bytes=0,completed_files=0,completed_bytes=0,current_file='',error='',local_deleted=False)
                self.state['jobs'].append(job);result.append(identifier)
            self.state['settings']=dict(config);self.state['paused']=False;self._save()
        return dict(queued=result)

    def retry(self,identifier):
        with self.lock:
            job=self._job(identifier)
            if job['state'] not in ('failed','paused','interrupted'):raise CloudError('当前任务不能重试')
            if self.credentials() is None and not job.get('cleanup_requested'):raise CloudError('请先保存平台凭据')
            job.update(state='cleanup_queued' if job.get('cleanup_requested') else 'queued',phase='等待重试',error='')
            self.state['paused']=False;self._save()
        return dict(queued=identifier)
    def control(self,action):
        if action not in ('pause','resume'):raise CloudError('上传控制无效')
        with self.lock:
            self.state['paused']=action=='pause'
            if action=='resume':
                for job in self.state['jobs']:
                    if job['state']=='paused':job.update(state='cleanup_queued' if job.get('cleanup_requested') else 'queued',phase='等待继续')
            self._save()
        return dict(paused=action=='pause')
    def can_cleanup(self,job,fresh=False):
        if job.get('local_deleted') or job['state']!='success':return False
        cached=self.cleanup_cache.get(job['id'])
        if not fresh and cached and time.monotonic()-cached[0]<5:return cached[1]
        directory=self.root/'jobs'/job['id'];receipt=read_json(directory/'receipt.json');manifest=read_json(directory/'manifest.json')
        if not receipt or not manifest or not receipt.get('platform_completion_acknowledged') or receipt.get('manifest_digest')!=manifest.get('digest'):return False
        try:valid=same_manifest(manifest,source_manifest(Path(job['source']),hashes=False),hashes=False)
        except (OSError,ValueError):valid=False
        self.cleanup_cache[job['id']]=(time.monotonic(),valid);return valid
    def cleanup(self,value):
        if not isinstance(value,dict) or set(value)!={'job_ids','confirmation'} or value['confirmation']!='DELETE_UPLOADED_LOCAL' or not isinstance(value['job_ids'],list) or not 1<=len(value['job_ids'])<=200:
            raise CloudError('请明确确认要清理的已上传本地批次')
        if self.collection_busy():raise CloudError('请结束采集准备或录制后再清理本地数据')
        with self.lock:
            jobs=[self._job(identifier) for identifier in value['job_ids']]
            if any(not self.can_cleanup(job,fresh=True) for job in jobs):raise CloudError('有批次尚未完整上传核验或本地内容已变化，禁止清理')
            for job in jobs:job.update(state='cleanup_queued',phase='等待清理本地副本',cleanup_requested=True,error='')
            self.state['paused']=False;self._save()
        return dict(queued=len(jobs))

    def status(self,day='today'):
        rows,_=self.store.entries('all')
        date=datetime.datetime.now(CST).date().isoformat() if day=='today' else day
        if date!='all':
            try:datetime.date.fromisoformat(date)
            except (ValueError,TypeError):raise CloudError('上传日期范围无效')
        for row in rows:row['day']=datetime.datetime.fromtimestamp(row['created_unix'],CST).date().isoformat()
        rows=[row for row in rows if date=='all' or row['day']==date]
        with self.lock:
            jobs=copy.deepcopy(self.state['jobs']);paused=self.state['paused'];config=dict(self.state['settings'])
        latest={job['session_id']:job for job in jobs}
        for row in rows:
            job=latest.get(row['id'])
            row['cloud']=dict(state=job['state'] if job else 'not_uploaded',job_id=job['id'] if job else None,
                can_cleanup=self.can_cleanup(job) if job else False,local_deleted=job.get('local_deleted',False) if job else False)
        visible=[job for job in jobs if day=='all' or job['recording'].get('day')==date]
        for job in visible:
            if job.get('local_deleted') and not any(row['id']==job['session_id'] for row in rows):
                row=dict(job['recording']);row['cloud']=dict(state='success',job_id=job['id'],can_cleanup=False,local_deleted=True);rows.append(row)
        public=[]
        for job in visible[-200:]:
            public.append({key:job.get(key) for key in ('id','session_id','state','phase','completed_files','total_files','completed_bytes','total_bytes','current_file','error','remote_path','completed_at','local_deleted')})
        return dict(credentials=self.credential_status(),settings=config,paused=paused,collection_busy=self.collection_busy(),jobs=public,recordings=rows,day=date)

    def _run(self):
        while not self.stop.wait(.25):
            try:
                if self.active:self._poll()
                else:
                    with self.lock:
                        if self.state['paused']:continue
                        job=next((j for j in self.state['jobs'] if j['state'] in ('queued','cleanup_queued')),None)
                    with self.lock:
                        if job and not self.stop.is_set() and not self.collection_busy():
                            try:self._launch(job)
                            except Exception as error:
                                job.update(state='failed',phase='需要重试',error=str(error) if isinstance(error,CloudError) else '上传进程未能启动，请重试');self._save()
            except Exception as error:
                with self.lock:
                    if self.active:
                        self.active.update(state='failed',phase='需要重试',error=str(error) if isinstance(error,CloudError) else '上传后台发生错误，本地数据保留，请重试')
                        if self.process and self.process.poll() is None:self.process.terminate()
                        self.active=None;self._save()
    def _launch(self,job):
        mode='cleanup' if job['state']=='cleanup_queued' else 'upload'
        if mode=='upload':
            meta=self.store.metadata(self.store.path(job['session_id']))
            if meta['token']!=job['source_token'] or not meta['retained']:
                with self.lock:job.update(state='failed',error='本地批次已变化，请刷新后重新选择',phase='需要核对');self._save()
                return
        credentials=self.credentials() if mode=='upload' else None
        if mode=='upload' and credentials is None:
            with self.lock:job.update(state='paused',phase='等待输入平台凭据');self._save()
            return
        directory=self.root/'jobs'/job['id'];directory.mkdir(parents=True,exist_ok=True)
        payload=dict(base=str(self.base),directory=str(directory),job=copy.deepcopy(job),mode=mode,credentials=credentials)
        python=Path('/home/orangepi/maintenance/uploader/venv/bin/python')
        process=subprocess.Popen([str(python),'-u',str(Path(__file__).with_name('cloud_worker.py'))],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,start_new_session=True)
        try:process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush();process.stdin.close()
        except Exception:
            process.terminate();process.wait(timeout=3);process.stdout.close();raise
        finally:payload.clear()
        with self.lock:self.process=process;self.active=job;self.pause_reason=None;self.last_save=0;job.update(state='cleaning' if mode=='cleanup' else 'checking',phase='检查本地文件',error='');self._save()
        def reader():
            for line in process.stdout:
                if len(line)>65536:continue
                try:value=json.loads(line)
                except ValueError:continue
                self.lines.put(value)
            self.lines.put(None)
        self.reader=threading.Thread(target=reader,name='cloud-events',daemon=True);self.reader.start()
    def _poll(self):
        job=self.active;process=self.process
        # A cleanup rename/delete transaction is completed once requested; it is
        # short and remains protected by the same recording mutation lease.
        if not job.get('cleanup_requested') and process.poll() is None and not self.pause_reason and (self.state['paused'] or self.collection_busy()):
            self.pause_reason='采集优先，已暂停上传；回到空闲后继续' if not self.state['paused'] else '已暂停，可继续上传'
            process.terminate()
        eof=False
        with self.lock:
            while True:
                try:event=self.lines.get_nowait()
                except queue.Empty:break
                if event is None:eof=True;continue
                allowed={'state','phase','completed_files','total_files','completed_bytes','total_bytes','current_file','error','completed_at','local_deleted','cleaned_at','platform_task_id'}
                for key in allowed:
                    if key in event:job[key]=event[key]
            if time.monotonic()-self.last_save>1:self._save();self.last_save=time.monotonic()
            if process.poll() is not None and (eof or not self.reader.is_alive()):
                self.reader.join(.1)
                receipt=read_json(self.root/'jobs'/job['id']/'receipt.json')
                if receipt and receipt.get('platform_completion_acknowledged') and not job.get('cleanup_requested'):
                    job.update(state='success',phase='已上传并核验，可清理本地数据',completed_at=receipt['completed_at'],
                               completed_files=receipt['verification']['files'],completed_bytes=receipt['verification']['bytes'])
                elif job['state']=='success' and not job.get('cleanup_requested'):
                    job.update(state='failed',phase='需要核对',error='进程结束但缺少完整上传回执，暂不可清理')
                if self.pause_reason and job['state']!='success':
                    job.update(state='paused' if self.state['paused'] else 'queued',phase=self.pause_reason,error='')
                elif process.returncode!=0 and job['state']!='failed':job.update(state='failed',phase='需要重试',error='传输进程中断，本地数据保留，请重试核验')
                elif job['state'] not in ('success','failed'):job.update(state='failed',phase='需要重试',error='未收到完整成功回执，本地数据保留')
                self._save();self.active=None;self.process=None;process.stdout.close()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(1)
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
            if self.active and self.active['state'] in ACTIVE:self.active.update(state='interrupted',phase='服务重启后可重试核验')
            self._save()
