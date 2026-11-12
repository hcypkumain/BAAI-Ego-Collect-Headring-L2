"""Read-only raw archives -> verified, fixed-profile Multi-Frequency LeRobot v3.1.

This worker never changes Pi data, QC decisions, the QC database, or raw archives.
Only successful signed archive receipts enter its independent export queue.
"""
import argparse,collections,datetime,fcntl,hashlib,hmac,json,os,pathlib,re,shutil,subprocess,sys,time,uuid
from dataclasses import dataclass
from collections.abc import Mapping
from importlib import import_module
from typing import Callable
from host.common.device_registry import Registry,receive_ids,parse_registry
from host.common.strict_json import strict_json_loads,canonical_json
P=pathlib.Path
ROOT=P(__file__).parent
VERSION='host-mf-v3.1-20261107-2-eeg8'

@dataclass(frozen=True)
class WorkerContext:
 registry: Registry
 config_json: str
 qc_root: P
 data_root: P
 state_root: P
 log_root: P
 executor: Callable = subprocess.run

 @property
 def config(self):return strict_json_loads(self.config_json)

def make_context(*,registry:Registry,config:Mapping,qc_root:P,data_root:P,state_root:P,log_root:P)->WorkerContext:
 receive_ids(registry)
 if not isinstance(config,Mapping):raise ValueError('Explicit storage config required')
 paths=tuple(P(p) for p in (qc_root,data_root,state_root,log_root))
 if any(not p.is_absolute() or '..' in p.parts for p in paths):raise ValueError('Explicit absolute context paths required')
 if config.get('data_root')!=str(paths[1]):raise ValueError('Context/config data root mismatch')
 return WorkerContext(registry,canonical_json(dict(config)),*paths)

def storage():
 # Lazy source import only; every storage call below supplies config and root.
 return import_module('services.headring-qc.host_storage')

def load(p):return strict_json_loads(P(p).read_text())
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(p):
 h=hashlib.sha256()
 with P(p).open('rb') as f:
  for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
 return h.hexdigest()
def atomic(path,value):
 path=P(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
 with temp.open('wb') as f:f.write(canonical(value));f.flush();os.fsync(f.fileno())
 os.replace(temp,path)
 fd=os.open(path.parent,os.O_RDONLY)
 try:os.fsync(fd)
 finally:os.close(fd)
def clean_rel(value):
 p=pathlib.PurePosixPath(value)
 if not value or p.is_absolute() or '..' in p.parts or '\\' in value or str(p)!=value or '\x00' in value:raise ValueError('Unsafe manifest path')
 return p
def verify_receipt(path,*,context):
 data=context.data_root;qc=context.qc_root
 path=P(path);r=load(path);did=r.get('device_id');sid=r.get('session_id','')
 if did not in receive_ids(context.registry) or not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]+',sid):raise ValueError('Unknown device/session')
 if r.get('passed') is not True or r.get('archive_verified') is not True:raise ValueError('QC not passed or archive not committed')
 if r.get('purpose')!='host_qc_archive_v1':raise ValueError('Wrong archive receipt purpose')
 identity=load(qc/'server-identity.json')['server_id']
 if r.get('server_id')!=identity:raise ValueError('Wrong archive server')
 signed={k:v for k,v in r.items() if k!='signature'}
 expected=hmac.new((qc/'secrets'/did).read_bytes(),canonical(signed),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,r.get('signature','')):raise ValueError('Archive receipt authentication failed')
 source=storage().storage_path(r['archive_path'],context.config,config_root=qc);resolved=source.resolve();archive=(data/'archive').resolve()
 try:rel=resolved.relative_to(archive)
 except ValueError:raise ValueError('Archive path escaped accepted data root')
 if len(rel.parts)!=3 or rel.parts[1:]!=(did,sid) or not re.fullmatch(r'20\d\d-\d\d-\d\d',rel.parts[0]):raise ValueError('Archive path does not match receipt')
 if source!=resolved or not source.is_dir() or source.is_symlink():raise ValueError('Archive links are not allowed')
 files=r['manifest']['files'];seen={}
 for row in files:
  name=clean_rel(row['path']).as_posix()
  if name in seen:raise ValueError('Duplicate archive manifest path')
  seen[name]=row
 if len(seen)!=r['manifest']['total_files']:raise ValueError('Archive manifest count mismatch')
 actual=set();total=0
 for parent,dirs,names in os.walk(source,followlinks=False):
  for n in dirs:
   if P(parent,n).is_symlink():raise ValueError('Link in raw archive')
  for name in names:
   p=P(parent,name);key=p.relative_to(source).as_posix()
   if p.is_symlink() or not p.is_file() or key not in seen:raise ValueError('Unexpected file in raw archive')
   row=seen[key];size=p.stat().st_size
   expected_size=row.get('bytes',row.get('size'))
   if size!=expected_size or digest(p)!=row['sha256']:raise ValueError('Raw archive checksum mismatch: '+key)
   actual.add(key);total+=size
 if actual!=set(seen) or total!=r['manifest']['total_bytes']:raise ValueError('Raw archive incomplete')
 return r,source,rel.parts[0]

def state_path(did,sid,*,context):return context.state_root/'jobs'/did/(sid+'.json')
def make_one(receipt_path,force=False,*,context):
 DATA=context.data_root;STATE=context.state_root;LOGS=context.log_root
 initial=load(receipt_path);did=initial.get('device_id');sid=initial.get('session_id','')
 if did not in receive_ids(context.registry):return {'state':'ignored_device'}
 if not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]+',sid):raise ValueError('Unsafe session name')
 sp=state_path(did,sid,context=context);old=load(sp) if sp.exists() else {}
 receipt_sha=digest(receipt_path)
 if old.get('state')=='complete' and old.get('archive_receipt_sha256')==receipt_sha and not force:return old
 if old.get('state')=='blocked' and old.get('archive_receipt_sha256')==receipt_sha and not force:return old
 row={'device':did,'session':sid,'version':VERSION,'archive_receipt_sha256':receipt_sha,'updated':time.time()}
 match=re.search(r'20\d\d-\d\d-\d\d',sid)
 if not match or match.group()<'2026-11-01':
  row.update(state='excluded_before_cutoff',reason='Only captures dated 2026-11-01 or later are selected');atomic(sp,row);return row
 if not initial.get('passed'):
  row.update(state='excluded_qc_failed',reason='原始数据保留在隔离区，不发布为合格标准包');atomic(sp,row);return row
 row.update(state='verifying_raw');atomic(sp,row)
 try:
  storage().require_storage(context.config,write_paths=(STATE,LOGS,DATA/'standardized'),config_root=context.qc_root)
  if shutil.disk_usage(DATA).free<35*1024**3:raise RuntimeError('LOW_SPACE: reserve at least 35 GiB')
  receipt,source,day=verify_receipt(receipt_path,context=context)
  target=DATA/'standardized'/did/day/sid
  if target.is_symlink() or target.parent.resolve()!=target.parent:raise ValueError('Unsafe export directory')
  profile='L3' if (source/'eeg/eeg.npz').is_file() else 'L2'
  row.update(state='converting',source=str(source),output=str(target),profile=profile,started=time.time());atomic(sp,row)
  log=LOGS/did/(sid+'.log');log.parent.mkdir(parents=True,exist_ok=True)
  env=dict(os.environ,PATH=str(ROOT/'video-bin')+os.pathsep+os.environ.get('PATH',''),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
  command=[str(ROOT/'venv/bin/python'),'-u',str(ROOT/'converter/standard_export.py'),str(source),'--out',str(target),'--profile',profile,'--source-digest',receipt['manifest']['digest'],'--workers','2']
  with log.open('w') as f:process=context.executor(command,stdout=f,stderr=subprocess.STDOUT,env=env,timeout=7200)
  if process.returncode:raise ValueError('Conversion refused; see '+str(log)+': '+log.read_text(errors='replace')[-1200:])
  results=[strict_json_loads(s[len('EXPORT_RESULT '):]) for s in log.read_text().splitlines() if s.startswith('EXPORT_RESULT ')]
  if len(results)!=1 or not results[0].get('validation',{}).get('passed'):raise ValueError('No valid export receipt')
  proof=load(target.with_name(target.name+'.receipt.json'))
  if proof['source_digest']!=receipt['manifest']['digest']:raise ValueError('Export source identity mismatch')
  # The exporter verifies all output hashes on cache hits and before publishing.
  row.update(state='complete',completed=time.time(),updated=time.time(),validation=results[0]['validation'],seconds=results[0]['seconds'],output_receipt_sha256=digest(target.with_name(target.name+'.receipt.json')))
 except Exception as e:
  row.update(state='blocked',updated=time.time(),reason=str(e)[:2000])
 atomic(sp,row);return row

def snapshot(*,context):
 STATE=context.state_root;DATA=context.data_root
 jobs=[]
 for path in (STATE/'jobs').glob('*/*.json'):
  try:
   row=load(path)
   if row.get('device') in receive_ids(context.registry):jobs.append(row)
  except (ValueError,OSError):continue
 return {'version':VERSION,'updated':time.time(),'counts':dict(collections.Counter(x['state'] for x in jobs)),'jobs':jobs,'output_root':str(DATA/'standardized')}

def run_pass(limit=0,force=False,*,context):
 STATE=context.state_root;DATA=context.data_root;LOGS=context.log_root
 storage().require_storage(context.config,write_paths=(STATE,LOGS,DATA/'standardized'),config_root=context.qc_root)
 STATE.mkdir(parents=True,exist_ok=True)
 with (STATE/'worker.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return {'state':'already_running'}
  paths=sorted((DATA/'receipts').glob('*/*.json'));done=0
  for path in paths:
   r=load(path)
   if r.get('device_id') not in receive_ids(context.registry):continue
   if not re.fullmatch(r'L[23]-[A-Za-z0-9_.-]+',r.get('session_id','')):raise ValueError('Unsafe session name')
   sp=state_path(r['device_id'],r['session_id'],context=context);old=load(sp) if sp.exists() else {}
   same=old.get('archive_receipt_sha256')==digest(path)
   if old.get('state') in ['complete','blocked','excluded_qc_failed','excluded_before_cutoff'] and same and not force:continue
   row=make_one(path,force,context=context);done+=1
   atomic(STATE/'status.json',snapshot(context=context));print(json.dumps({k:v for k,v in row.items() if k not in ['validation']},ensure_ascii=False),flush=True)
   if limit and done>=limit:break
  result=snapshot(context=context);atomic(STATE/'status.json',result)
  # Legacy catalog has a separate configuration loader; do not launch it with
  # an implicit inventory different from this context.
  return result

def main():
 p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');p.add_argument('--status',action='store_true');p.add_argument('--limit',type=int,default=0);p.add_argument('--retry-blocked',action='store_true');p.add_argument('--receipt')
 for name in ('registry','config','qc-root','data-root','state-root','log-root'):p.add_argument('--'+name,type=P,required=True)
 a=p.parse_args()
 context=make_context(registry=parse_registry(load(a.registry)),config=load(a.config),qc_root=a.qc_root,data_root=a.data_root,state_root=a.state_root,log_root=a.log_root)
 if a.status:print(json.dumps(snapshot(context=context),ensure_ascii=False));return
 if a.receipt:print(json.dumps(make_one(P(a.receipt),a.retry_blocked,context=context),ensure_ascii=False));return
 while True:
  r=run_pass(a.limit,a.retry_blocked,context=context);print(json.dumps({'pass_counts':r.get('counts'),'state':r.get('state')},ensure_ascii=False),flush=True)
  if not a.watch:break
  time.sleep(60)
if __name__=='__main__':main()
