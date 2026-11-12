"""Read-only, bounded recording metadata inventory; never transfer raw payloads."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3

REMOTE = r'''import glob,hashlib,json,os,re,sys
from pathlib import Path
device=sys.argv[1]; project=Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')
actual=Path('/opt/egorecord/DEVICE_ID').read_text().strip()
if actual!=device: raise SystemExit('device identity mismatch')
result={'device':actual,'roots':[],'sessions':[],'errors':[],'truncated':False}
roots=[]
configs=[project/'orangepi/configs/storage.yaml']+[Path(p) for p in glob.glob(str(project/'profiles/*/orangepi/configs/storage.yaml'))]
for config in configs:
 try:
  import yaml
  x=yaml.safe_load(config.read_text()) or {}
  mount=x.get('mount_point'); directory=x.get('directory')
  if isinstance(mount,str) and mount.startswith('/media/orangepi/') and '..' not in Path(mount).parts and isinstance(directory,str) and directory and '/' not in directory and directory not in ('.','..'):
   roots.append(Path(mount)/directory)
  else: result['errors'].append({'config':str(config),'error':'unreviewed storage path'})
 except Exception as e: result['errors'].append({'config':str(config),'error':type(e).__name__})
if device=='cp_0018':
 roots += [Path('/media/orangepi/sdcard/HeadRing-V81-TEST/TEST-cp0018/data/TEST-only'),Path('/media/orangepi/sdcard/HeadRing-V82-FULLSTACK-TEST/TEST-cp0018/data/TEST-only')]
seen=set(); budget=5000
def visit(path,depth):
 global budget
 if budget<=0: result['truncated']=True; return
 if path.is_symlink() or not path.is_dir(): return
 budget-=1
 if re.fullmatch(r'L2-[A-Za-z0-9-]{5,100}',path.name):
  item={'session':path.name,'path':str(path),'state':'unknown','native_sha256':None,'metadata':[]}
  for name in ('native_capture_summary.json','capture_summary.json','meta.json','status.json','manifest.json','raw_manifest.json'):
   p=path/name
   try:
    if not p.is_symlink() and p.is_file():
     s=p.stat(); row={'name':name,'size':s.st_size}
     if name=='native_capture_summary.json' and s.st_size<=2097152:
      raw=p.read_bytes(); item['native_sha256']=hashlib.sha256(raw).hexdigest()
     item['metadata'].append(row)
   except OSError as e: result['errors'].append({'path':str(p),'error':type(e).__name__})
  result['sessions'].append(item); return
 if depth<=0: return
 try:
  with os.scandir(path) as it:
   entries=[]
   for ent in it:
    if len(entries)>=1000: result['truncated']=True; break
    if ent.is_dir(follow_symlinks=False): entries.append(Path(ent.path))
  for child in sorted(entries): visit(child,depth-1)
 except OSError as e: result['errors'].append({'path':str(path),'error':type(e).__name__})
for root in roots:
 if str(root) in seen: continue
 seen.add(str(root)); result['roots'].append({'path':str(root),'exists':root.is_dir() and not root.is_symlink()}); visit(root,5)
result['state_counts']={k:sum(1 for x in result['sessions'] if x['state']==k) for k in ('current','active','sealed','pending','unknown')}
print(json.dumps(result,sort_keys=True))
'''

DATABASES = [Path.home()/'headring-qc/state/receiver/receiver.sqlite3',
             Path.home()/'Documents/Codex/2026-11-09/ianz/work/recovery-service/state/receiver/receiver.sqlite3',
             Path.home()/'Documents/Codex/2026-11-09/ianz/work/today-2026-11-10/data/state/receiver/receiver.sqlite3']

def receiver_evidence(device, databases=DATABASES):
    result=[]
    for path in databases:
        row={'database':str(path),'matches':[], 'status':'unknown'}
        try:
            # immutable prevents SQLite from creating or modifying WAL/SHM files.
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
                rows=db.execute('select session from jobs where device=?',(device,)).fetchall()
            row.update(status='metadata_only',matches=[x[0] for x in rows],wal_may_be_unread=True)
        except (OSError,sqlite3.Error) as exc: row['error']=type(exc).__name__
        result.append(row)
    return result

def local_evidence(device, session, native_sha, roots):
    if not re.fullmatch(r'cp_[0-9]{4}',device) or not re.fullmatch(r'L2-[A-Za-z0-9-]{5,100}',session or ''):
        return {'status':'unknown','reason':'invalid source identity'}
    if not re.fullmatch(r'[0-9a-f]{64}',native_sha or ''): return {'status':'unknown','reason':'source native identity unavailable'}
    seen=[]
    for root in roots:
        root=Path(root)
        # Only explicit candidate layouts; never recursively scan a volume.
        for path in (root/session/'native_capture_summary.json',root/device/session/'native_capture_summary.json'):
            try:
                parts=path.relative_to(root).parts; current=root
                unsafe=root.is_symlink()
                for part in parts:
                    current=current/part
                    if current.is_symlink(): unsafe=True; break
                if unsafe: continue
                if path.is_symlink() or not path.is_file() or path.stat().st_size>2097152: continue
                actual=hashlib.sha256(path.read_bytes()).hexdigest()
                seen.append({'path':str(path),'native_sha256':actual,'matches_native':actual==native_sha})
            except OSError: continue
    if any(x['matches_native'] for x in seen):
        return {'status':'metadata_only','candidates':seen,'note':'native metadata match is not proof of complete raw backup'}
    return {'status':'unknown','candidates':seen,'reason':'no identity-matched raw backup proof'}

def audit(ssh,device,local_roots):
    encoded=base64.b64encode(REMOTE.encode()).decode()
    command='python3 -c '+shlex.quote('import base64;exec(compile(base64.b64decode('+repr(encoded)+'),"audit","exec"))')+' '+shlex.quote(device)
    result=json.loads(ssh.run(command))
    if result.get('device')!=device: raise ValueError('device identity mismatch')
    result['receiver_databases']=receiver_evidence(device)
    for row in result.get('sessions',[]): row['local_backup']=local_evidence(device,row['session'],row.get('native_sha256'),local_roots)
    result['fresh_full_hash']=False
    return result
