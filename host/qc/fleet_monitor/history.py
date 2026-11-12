"""Independent metadata index. Reads collector stores without changing them."""
from pathlib import Path
from contextlib import contextmanager
import json,sqlite3,time,threading,re,datetime,collections,hashlib

ZONE=datetime.timezone(datetime.timedelta(hours=8))
def day_of(value):
    try:return datetime.datetime.fromtimestamp(float(value),ZONE).strftime('%Y-%m-%d')
    except (ValueError,TypeError,OverflowError):return ''

def session_time(name):
    m=re.search(r'(20\d\d-\d\d-\d\d-\d\d-\d\d-\d\d)',name or '')
    if not m:return None
    try:return datetime.datetime.strptime(m.group(1),'%Y-%m-%d-%H-%M-%S').replace(tzinfo=ZONE).timestamp()
    except ValueError:return None

def safe_json(value,default=None):
    try:return json.loads(value)
    except (ValueError,TypeError):return {} if default is None else default

@contextmanager
def connection(*args,**kwargs):
    # SQLite's transaction context manager does not close the connection.
    c=sqlite3.connect(*args,**kwargs)
    try:
        with c:yield c
    finally:c.close()

def ro(path):return connection('file:'+str(path)+'?mode=ro',uri=True,timeout=2)

class History:
    def __init__(self,app,state):
        self.app=Path(app);self.config=json.loads((self.app/'config.json').read_text());self.data=Path(self.config['data_root'])
        self.path=Path(state)/'history.sqlite3';self.lock=threading.RLock();self.records={};self.legacy_keys=set();self.packages_seen={};self.package_keys={};self.signatures={};self.ready=False;self.error=None;self.last_success=None
        with connection(self.path) as c:
            c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE IF NOT EXISTS captures(device TEXT,session TEXT,record TEXT NOT NULL,PRIMARY KEY(device,session))')
            for device,session,record in c.execute('SELECT device,session,record FROM captures').fetchall():
                r=safe_json(record)
                if not any(r.get(k) for k in ('source','saved','standardized','image_name','task_name','outcome')):
                    c.execute('DELETE FROM captures WHERE device=? AND session=?',(device,session))
                else:self.records[(device,session)]=r

    def upsert(self,row):
        if not re.fullmatch(r'cp_\d{4}',str(row.get('device',''))) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',str(row.get('session',''))):return
        key=(row['device'],row['session'])
        with self.lock:
            previous=self.records.get(key,{});value=dict(previous)
            for k,v in row.items():
                if v is not None and v!='':
                    if k in ('saved','standardized') and previous.get(k) is True and v is False:continue
                    value[k]=v
            value.setdefault('created',session_time(value['session']))
            value['day']=day_of(value.get('created'))
            value.setdefault('mode','eeg' if value['session'].startswith('L3-') else 'emg')
            text=(value.get('image_name','')+' '+value.get('task_name','')).upper()
            if 'HANDOFF_V5_' in text or value['session'].upper().startswith(('SMOKE','TEST-')):value['test']=True
            if value==previous:return
            self.records[key]=value
            with connection(self.path,timeout=2) as c:c.execute('INSERT OR REPLACE INTO captures VALUES(?,?,?)',(key[0],key[1],json.dumps(value,ensure_ascii=False,separators=(',',':'))))

    def _changed(self,name,path):
        try:
            p=Path(path);stat=p.stat();wal=p.with_name(p.name+'-wal');ws=wal.stat() if wal.exists() else None
            signature=(stat.st_mtime_ns,stat.st_size,ws.st_mtime_ns if ws else 0,ws.st_size if ws else 0)
        except OSError:return False
        if self.signatures.get(name)==signature:return False
        self.signatures[name]=signature;return True

    def _catalog(self):
        path=self.data/'standardized/_catalog.json'
        if not path.is_file():raise FileNotFoundError('standardized catalog unavailable')
        if not self._changed('catalog',path):return
        catalog=safe_json(path.read_text());self.catalog_count=catalog.get('packages')
        for entry in catalog.get('entries',[]):
            relative=Path(entry.get('relative_path',''))
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:continue
            device=relative.parts[0];session=entry.get('original_session') or relative.name
            if not re.fullmatch(r'cp_\d{4}',device):continue
            self.package_keys[str(self.data/'standardized'/relative)]=(device,session)
            row={'device':device,'session':session,'mode':'eeg' if entry.get('base_profile')=='L3' else 'emg',
                 'standardized':True,'saved':True,'outcome':'success','source':'standardized'}
            info=self.data/'standardized'/relative/'meta/collect_info.jsonl'
            if str(relative) not in self.packages_seen:
                self.packages_seen[str(relative)]=True
                try:
                    with info.open() as f:metadata=safe_json(f.readline(262144))
                    m=metadata.get('source_metadata') or {};selection=m.get('reference_selection') or {};asset=selection.get('asset') or {};lists=asset.get('source_lists') or []
                    task=(m.get('reference_bundle') or {}).get('task') or (lists[0].get('name') if lists else None) or metadata.get('task_name')
                    row.update(task_name=task,image_name=asset.get('name'),image_key=asset.get('id') or m.get('reference_sha256') or asset.get('name'),
                               created=m.get('session_created_unix'),mode=m.get('collection_mode') or row['mode'],test=m.get('test') is True)
                    if selection.get('list_name')==task:row['plan_total']=selection.get('total')
                    quality_file=info.with_name('qc_reports.jsonl')
                    if quality_file.exists():
                        with quality_file.open() as q:quality=safe_json(q.readline(524288))
                        level=quality.get('level') or (quality.get('qc_report') or {}).get('level')
                        if level in ('INFO','WARN','ERROR'):row['level']=level
                except (OSError,ValueError,TypeError):pass
            self.upsert(row)

    def _legacy(self):
        path=self.data/'state/queue.sqlite3'
        if not path.is_file():raise FileNotFoundError('legacy ledger unavailable')
        if not self._changed('legacy',path):return
        with ro(path) as c:
            rows=c.execute("SELECT device,session,state,level,created,json_extract(details,'$.reference_name'),json_extract(details,'$.task_name'),json_extract(details,'$.reference_sha256') FROM jobs").fetchall()
        self.legacy_keys={(row[0],row[1]) for row in rows}
        for device,session,state,level,created,image,task,image_key in rows:
            self.upsert({'device':device,'session':session,'image_name':image,'task_name':task,'image_key':image_key or image,
                         'level':level,'process_state':state,'saved':True,'created':session_time(session) or created,'source':'receiver-history'})

    def _proof(self,device,session,details,record):
        """Check existing receipt and QC evidence; never infer QC from Pi or a package."""
        if record.get('process_state')=='owner_removed':return False,None
        try:
            raw=record.get('raw_receipt') or {};manifest=raw.get('manifest') or {};native=details.get('native_sha256')
            path=Path(record.get('raw_path') or '/nonexistent')
            files=manifest.get('files') or []
            valid=bool(record.get('receive_state')=='host_committed' and raw.get('host_verified') and raw.get('device_id')==device and raw.get('session_id')==session and native and manifest.get('native_sha256')==native and files and path.is_dir())
            if valid:
                root=path.resolve()
                for item in files:
                    target=path/item['path']
                    if target.is_symlink() or root not in target.resolve().parents or not target.is_file() or target.stat().st_size!=item['bytes']:
                        valid=False;break
        except (OSError,ValueError,TypeError,KeyError):valid=False
        try:
            native=details.get('native_sha256')
            result_path=self.data/'results'/device/session/'result.json';report_path=result_path.parent/'artifacts/qc_report.json'
            result_bytes=result_path.read_bytes();result=json.loads(result_bytes);digest=hashlib.sha256(result_bytes).hexdigest()
            report_bytes=report_path.read_bytes();report_digest=hashlib.sha256(report_bytes).hexdigest();report=json.loads(report_bytes)
            qc=(record.get('process_state')=='complete' and result==record.get('result') and digest==record.get('result_sha256') and digest==(record.get('archive_receipt') or {}).get('result_sha256') and result.get('session_id')==session and result.get('native_sha256')==native and result.get('request_id')==details.get('request_id') and result.get('level') in ('INFO','WARN','ERROR') and report_digest==(result.get('artifacts') or {}).get('qc_report.json') and report.get('level')==result.get('level'))
            return valid,result['level'] if qc else None
        except (OSError,ValueError,TypeError,KeyError):return valid,None

    def _manual(self):
        path=self.app/'state/receiver/receiver.sqlite3'
        self._changed('manual',path)
        with ro(path) as c:rows=c.execute('SELECT device,session,details,record FROM jobs').fetchall()
        for device,session,details,record in rows:
            d=safe_json(details);r=safe_json(record);local_ok,pc_level=self._proof(device,session,d,r)
            self.upsert({'device':device,'session':session,'image_name':d.get('reference_name'),'task_name':d.get('task_name'),
                         'image_key':d.get('reference_sha256') or d.get('reference_name'),'created':session_time(session),
                         'saved':True,'level':r.get('level'),'outcome':r.get('capture_outcome'),
                         'process_state':r.get('process_state'),'standardized':r.get('standard_state') in ('complete','published'),
                         'cloud_state':r.get('cloud_state'),'removed':r.get('process_state')=='owner_removed','source':'manual-receiver',
                         'local_complete':local_ok,'pc_qc_level':pc_level or 'unknown'})

    def _cloud(self):
        path=self.app/'integrations/robot-uploader-1.0.0/adapter/cloud-jobs/cloud-upload.sqlite3'
        if not path.exists():return
        with ro(path) as c:rows=c.execute('SELECT target,state,source FROM uploads').fetchall()
        for target,state,source in rows:
            source_path=Path(source)
            for old,new in self.config.get('storage_relocations',{}).items():
                try:source_path=Path(new)/source_path.relative_to(old);break
                except ValueError:pass
            key=self.package_keys.get(str(source_path))
            if not key:
                parts=Path(target).parts;device=next((p for p in parts if re.fullmatch(r'cp_\d{4}',p)),None)
                candidate=(device,parts[-1]) if device and parts else None
                key=candidate if candidate in self.records else None
            if key:self.upsert({'device':key[0],'session':key[1],'cloud_state':state})

    def sync(self):
        errors=[]
        for fn in (self._catalog,self._legacy,self._manual,self._cloud):
            try:fn()
            except (OSError,sqlite3.Error,ValueError,TypeError) as e:
                errors.append(fn.__name__+': '+type(e).__name__);self.signatures.clear()
        self.error='; '.join(errors) if errors else None
        self.ready=not errors
        if not errors:self.last_success=time.time()

    def records_for(self,device=None,period='all',mode='all',task=None):
        today=day_of(time.time())
        with self.lock:rows=[dict(r) for r in self.records.values()]
        rows=[r for r in rows if not r.get('test') and (not device or r['device']==device) and
              (period=='all' or r.get('day')==(today if period=='today' else period)) and (mode=='all' or r.get('mode')==mode) and
              (not task or (r.get('task_name') or '未记录任务名称')==task)]
        return sorted(rows,key=lambda r:float(r.get('created') or 0),reverse=True)

    @staticmethod
    def confirmed(r):return not r.get('removed') and r.get('pc_qc_level') in ('INFO','WARN')

    def summaries(self):
        counts={}
        for device in [d['id'] for d in self.config['devices']]:
            rows=self.records_for(device);today=[r for r in rows if r.get('day')==day_of(time.time())]
            images={(r.get('mode'),r.get('task_name'),r.get('image_key') or r.get('image_name')) for r in rows if self.confirmed(r) and (r.get('image_key') or r.get('image_name'))}
            counts[device]={'captures':len(rows),'saved':sum(bool(r.get('saved')) and not r.get('removed') for r in rows),
                            'today_saved':sum(bool(r.get('saved')) and not r.get('removed') for r in today),
                            'today_local_complete':sum(r.get('local_complete') is True and not r.get('removed') for r in today),
                            'today_pc_qc_done':sum(r.get('pc_qc_level') in ('INFO','WARN','ERROR') and not r.get('removed') for r in today),
                            'today_pc_qc_error':sum(r.get('pc_qc_level')=='ERROR' and not r.get('removed') for r in today),
                            'completed_images':len(images),'standardized':sum(bool(r.get('standardized')) and not r.get('removed') for r in rows),
                            'cloud_confirmed':sum(r.get('cloud_state')=='remote_confirmed' for r in rows)}
        return counts

    def tasks(self,device,period='all',mode='all',page=1,limit=20):
        groups={}
        for r in self.records_for(device,period,mode):
            task=r.get('task_name') or '未记录任务名称';key=(r.get('mode'),task)
            group=groups.setdefault(key,{'task_name':task,'mode':r.get('mode'),'saved':0,'captures':0,'retakes':0,'standardized':0,'cloud_confirmed':0,'plan_total':None,'updated':0,'images':set(),'seen_images':collections.Counter()})
            group['captures']+=1;group['saved']+=int(bool(r.get('saved')) and not r.get('removed'))
            group['standardized']+=int(bool(r.get('standardized')) and not r.get('removed'));group['cloud_confirmed']+=int(r.get('cloud_state')=='remote_confirmed')
            image=r.get('image_key') or r.get('image_name')
            if image:group['seen_images'][image]+=1
            if image and self.confirmed(r):group['images'].add(image)
            if r.get('plan_total'):group['plan_total']=r['plan_total']
            group['updated']=max(group['updated'],float(r.get('created') or 0))
        values=[]
        for g in groups.values():
            g['completed_images']=len(g.pop('images'));g['retakes']=sum(max(0,n-1) for n in g.pop('seen_images').values());values.append(g)
        values.sort(key=lambda x:x['updated'],reverse=True)
        return {'items':values[(page-1)*limit:page*limit],'total':len(values),'page':page,'limit':limit,'ready':self.ready,'error':self.error,'last_success':self.last_success}

    def sessions(self,device,period='all',mode='all',page=1,limit=20,task=None):
        rows=self.records_for(device,period,mode,task)
        fields=('device','session','mode','task_name','image_name','created','day','outcome','saved','level','standardized','cloud_state','process_state','removed','local_complete','pc_qc_level')
        return {'items':[{k:r.get(k) for k in fields} for r in rows[(page-1)*limit:page*limit]],'total':len(rows),'page':page,'limit':limit,'ready':self.ready,'error':self.error,'last_success':self.last_success}

    def receiver(self,page=1,limit=20,device=None):
        path=self.app/'state/receiver/receiver.sqlite3'
        try:
            with ro(path) as c:
                c.row_factory=sqlite3.Row;control=dict(c.execute('SELECT * FROM control WHERE id=1').fetchone())
                rows=c.execute('SELECT device,session,details,record FROM jobs ORDER BY id DESC').fetchall()
            visible=[]
            for row in rows:
                d=safe_json(row['details']);r=safe_json(row['record'])
                value={k:r.get(k) for k in ('receive_state','release_state','process_state','reply_state','standard_state','level','cloud_state','pc_delete_after','receive_error','release_error','process_error','reply_error')}
                value.update(device=row['device'],session=row['session'],reference_name=d.get('reference_name'),bytes=d.get('bytes',0));proof=self.records.get((row['device'],row['session']),{});value['level']=proof.get('pc_qc_level') if proof.get('pc_qc_level') in ('INFO','WARN','ERROR') else None;visible.append(value)
            manual_keys={(r['device'],r['session']) for r in visible}
            with self.lock: legacy=[dict(self.records[k]) for k in self.legacy_keys if k in self.records and k not in manual_keys]
            for row in legacy:
                visible.append(dict(device=row['device'],session=row['session'],reference_name=row.get('image_name'),bytes=0,receive_state='legacy_unknown',release_state=None,process_state='legacy_unknown',reply_state=None,standard_state='complete' if row.get('standardized') else None,level=None,cloud_state=row.get('cloud_state'),pc_delete_after=None,receive_error=None,release_error=None,process_error='旧队列：本机接收与 PC 质检证据未核验',reply_error=None))
            if device:visible=[r for r in visible if r['device']==device]
            summary={'total':len(visible),'received':sum(self.records.get((r['device'],r['session']),{}).get('local_complete') is True for r in visible),'released':sum(r['release_state']=='deleted' for r in visible),
                     'processed':sum(self.records.get((r['device'],r['session']),{}).get('pc_qc_level') in ('INFO','WARN','ERROR') for r in visible),'cloud':sum(r['cloud_state']=='remote_confirmed' for r in visible)}
            control['receiving']=bool(control['receiving']);control['stopping']=not control['receiving'] and any(r['receive_state']=='running' or r['release_state']=='running' for r in visible)
            return {'control':control,'summary':summary,'items':visible[(page-1)*limit:page*limit],'total':len(visible),'page':page,'limit':limit}
        except (OSError,sqlite3.Error,TypeError):return {'control':None,'summary':{},'items':[],'error':'接收状态暂不可用'}
