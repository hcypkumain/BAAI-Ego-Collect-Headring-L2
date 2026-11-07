"""Original per-scene used.yaml contract, atomic updates and preserved failed takes."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0,str(_Path(__file__).resolve().parents[1]/'common'))
import project_paths
import fcntl,hashlib,json,os,re,time
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

def valid_used(entry):
    if isinstance(entry,dict) and entry.get('status') in BAD:return False
    if isinstance(entry,dict) and entry.get('session'):
        if (Path(entry['session'])/'recording_deleted.json').is_file():return False
        meta=Path(entry['session'])/'meta.yaml'
        if meta.is_file() and (_load_yaml(meta.read_text(encoding='utf-8')) or {}).get('status') in BAD:return False
    return True

def images():
    return sorted(p for p in LIBRARY.rglob('*.png') if re.search(r'(?:_\d+|\d+_combo\d+_r\d+)$',p.stem))

def unused():
    result=[];cache={}
    for image in images():
        ledger=image.parent/'used.yaml'
        if ledger not in cache:cache[ledger]=read(ledger).get('used',{})
        entry=cache[ledger].get(image.name)
        if entry is None or not valid_used(entry):result.append(image)
    return result

def mark(image,session,status):
    image=Path(image).resolve();image.relative_to(LIBRARY.resolve())
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
            if not (status=='pending_qc' and isinstance(existing,dict) and existing.get('status')!='pending_qc' and valid_used(existing)):
                used[image.name]={'session':str(session),'at':time.strftime('%Y-%m-%d %H:%M:%S'),'status':status,'qc_level':quality}
            value.get('failed',{}).pop(image.name,None)
        else:
            if image.name in used and (not valid_used(used[image.name]) or used[image.name].get('session')==str(session)):
                used.pop(image.name,None)
            value.setdefault('failed',{})[image.name]={'session':str(session),'at':time.strftime('%Y-%m-%d %H:%M:%S'),'status':status}
        atomic_yaml(target,value)

def migrate_legacy():
    old=BASE/'runtime/used.json';done=BASE/'runtime/ledger_migration_v4.json'
    if done.exists():return
    migrated=[]
    if old.exists():
        for rel,entry in json.loads(old.read_text(encoding='utf-8')).items():
            image=PROJECT/rel;session=Path(entry['session']);meta=session/'meta.yaml'
            if image.exists() and meta.exists() and (_load_yaml(meta.read_text(encoding='utf-8')) or {}).get('status')=='success':
                existing=read(image.parent/'used.yaml').get('used',{}).get(image.name)
                if existing is None or not valid_used(existing):mark(image,session,'success')
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
