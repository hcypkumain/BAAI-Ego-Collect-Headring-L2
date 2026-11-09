"""Explicit, backed-up ledger edits. Preview by default; --apply performs writes."""
import sys,json,time,shutil,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src/orangepi_collect/common'))
from project_paths import BASE,PROJECT,CODE_BASE
import ledger

def rebase_value(value,old,new):
    changed=0
    if isinstance(value,dict):
        for key,item in value.items():
            if key=='session' and isinstance(item,str):
                try:relative=Path(item).relative_to(old)
                except ValueError:continue
                value[key]=str(new/relative);changed+=1
            else:changed+=rebase_value(item,old,new)
    elif isinstance(value,list):
        for item in value:changed+=rebase_value(item,old,new)
    return changed

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['list','reset-scene','rebase'])
    p.add_argument('--scene');p.add_argument('--old-root',type=Path);p.add_argument('--new-root',type=Path);p.add_argument('--apply',action='store_true');a=p.parse_args()
    if a.action=='list':
        for image in ledger.unused():print(image.relative_to(ledger.LIBRARY))
        sys.exit(0)
    if a.apply:
        # Refuse changes while any current launcher holds its lock, including manual instances.
        import fcntl
        (CODE_BASE/'runtime').mkdir(exist_ok=True);lock=(CODE_BASE/'runtime/launcher.lock').open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:p.error('先停止采集程序，再修改台账')
    if a.action=='reset-scene':
        if not a.scene:p.error('--scene is required')
        scene=(ledger.LIBRARY/a.scene).resolve();scene.relative_to(ledger.LIBRARY.resolve())
        paths=[scene/'used.yaml']
    else:
        if not a.old_root or not a.new_root:p.error('--old-root and --new-root are required')
        paths=sorted(ledger.LIBRARY.rglob('used.yaml'))
    edits=[]
    for path in paths:
        if not path.exists():continue
        value=ledger.read(path)
        if a.action=='reset-scene':
            count=len(value.get('used',{}))+len(value.get('failed',{}));value['used']={};value.pop('failed',None)
        else:count=rebase_value(value,a.old_root.resolve(),a.new_root.resolve())
        if count:edits.append((path,value,count))
    print(json.dumps({'apply':a.apply,'files':[{'path':str(p),'entries':n} for p,v,n in edits]},ensure_ascii=False,indent=2))
    if a.apply and edits:
        backup=BASE/'runtime/ledger_backups'/('manual_'+str(time.time_ns()));backup.mkdir(parents=True)
        for path,value,count in edits:
            saved=backup/path.relative_to(ledger.LIBRARY);saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,saved)
            ledger.atomic_yaml(path,value)
        print('Backup: '+str(backup))
