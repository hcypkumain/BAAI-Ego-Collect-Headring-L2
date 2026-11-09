#!/usr/bin/env python3
"""Checked, idle-only integrated release; one transaction with automatic rollback."""
import contextlib,fcntl,hashlib,json,os,pathlib,shutil,subprocess,tempfile,time,urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
PROJECT=pathlib.Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')
BASE=PROJECT/'orangepi'
MANIFEST=json.loads((ROOT/'manifest.json').read_text())
UNITS=['embodied-pi-collect.service','usb-phone-link.service','headring-usb-data-host.service']
TIMER='collector-usb-task-mount.timer'

def sha(p):
    p=pathlib.Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None

def run(args,check=True,timeout=60):
    return subprocess.run(args,capture_output=True,text=True,check=check,timeout=timeout)

def snapshot():
    token=(BASE/'runtime/dashboard_token').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
    with urllib.request.urlopen(req,timeout=5) as reply:return json.load(reply)

def idle(v):
    p=v.get('privacy') or {};w=v.get('worker') or {}
    return (v.get('state') in ('device_confirm','idle','failed_wait','exhausted')
        and not v.get('session') and not v.get('busy')
        and w.get('recording_active') is not True
        and w.get('state') not in ('recording','saving','checking','finalizing','postprocessing')
        and p.get('required') is False and p.get('phase')=='idle' and p.get('stop_confirmed') is True)

@contextlib.contextmanager
def capture_locks():
    handles=[];seen=set()
    try:
        for base in [BASE,PROJECT/'profiles/emg/orangepi',PROJECT/'profiles/eeg/orangepi']:
            p=base/'runtime/capture.lock'
            if not p.exists() or str(p.resolve()) in seen:continue
            seen.add(str(p.resolve()));f=p.open('r+');handles.append(f)
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        for f in reversed(handles):f.close()

class Transaction:
    def __init__(self):
        self.backup=ROOT/'backups'/time.strftime('%Y%m%d-%H%M%S')
        self.backup.mkdir(parents=True,exist_ok=False);os.chmod(self.backup,0o700);self.entries=[]
        self.units={n:dict(active=run(['systemctl','is-active',n],False).stdout.strip(),enabled=run(['systemctl','is-enabled',n],False).stdout.strip()) for n in UNITS+[TIMER]}
        (self.backup/'units.json').write_text(json.dumps(self.units,indent=2))
    def save(self,p):
        if any(x['path']==str(p) for x in self.entries):return
        if p.is_symlink():raise RuntimeError('Refusing symlink '+str(p))
        row=dict(path=str(p),exists=p.exists())
        if p.exists():
            st=p.stat();row.update(uid=st.st_uid,gid=st.st_gid,mode=st.st_mode&0o777)
            dest=self.backup/str(p).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
        self.entries.append(row);(self.backup/'index.json').write_text(json.dumps(self.entries,indent=2))
    def write(self,src,p,mode=0o644):
        if sha(src)==sha(p):return
        self.save(p);p.parent.mkdir(parents=True,exist_ok=True)
        old=p.stat() if p.exists() else p.parent.stat()
        fd,name=tempfile.mkstemp(prefix='.'+p.name,dir=str(p.parent))
        try:
            with os.fdopen(fd,'wb') as f:f.write(src.read_bytes());f.flush();os.fsync(f.fileno())
            os.chmod(name,mode);os.chown(name,old.st_uid,old.st_gid);os.replace(name,p)
        finally:
            if os.path.exists(name):os.unlink(name)
    def remove(self,p):
        if p.exists():self.save(p);p.unlink()
    def rollback(self):
        for n in UNITS+[TIMER]:run(['systemctl','stop',n],False)
        for row in reversed(self.entries):
            p=pathlib.Path(row['path'])
            if row['exists']:
                shutil.copy2(self.backup/str(p).lstrip('/'),p)
                os.chown(p,row['uid'],row['gid']);os.chmod(p,row['mode'])
            elif p.exists():p.unlink()
        run(['systemctl','daemon-reload'],False);run(['udevadm','control','--reload-rules'],False)
        for n,v in self.units.items():
            if n==TIMER and v['enabled']!='enabled':run(['systemctl','disable',n],False)
            if v['active']=='active':run(['systemctl','start',n],False)

def verify():
    device=pathlib.Path('/opt/egorecord/DEVICE_ID').read_text().strip()
    if device not in MANIFEST['allowed_devices']:raise RuntimeError('Device outside release scope')
    for row in MANIFEST['collector']:
        src=ROOT/'collector'/row['relative'];p=PROJECT/row['relative']
        if sha(src)!=row['after']:raise RuntimeError('Package mismatch '+str(src))
        if p.is_symlink() or sha(p) not in (row['before'],row['after']):raise RuntimeError('Live source changed '+str(p))
    for row in MANIFEST['system']:
        if sha(ROOT/'system'/row['name'])!=row['after']:raise RuntimeError('System package mismatch')
        p=pathlib.Path(row['target'])
        if p.is_symlink() or sha(p) not in (row['before'],row['after']):raise RuntimeError('Live system changed '+str(p))
    for row in MANIFEST['remove']:
        if sha(row['target']) not in (row['before'],None):raise RuntimeError('Binding config changed')
    return device

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    device=verify();v=snapshot()
    if args.check:
        print(json.dumps(dict(device=device,version=MANIFEST['version'],verified=True,state=v.get('state'),idle=idle(v))));return
    if os.geteuid()!=0:raise RuntimeError('Administrator authentication required')
    if not idle(v):raise RuntimeError('Deployment waits until empty idle workflow')
    tx=None
    try:
        with capture_locks():
            verify()
            if not idle(snapshot()):raise RuntimeError('Workflow became busy')
            tx=Transaction()
            # Stop capture entry points while holding the capture-start leases.
            for n in UNITS+[TIMER]:run(['systemctl','stop',n],check=n!=TIMER)
            for row in MANIFEST['collector']:tx.write(ROOT/'collector'/row['relative'],PROJECT/row['relative'])
            for row in MANIFEST['system']:tx.write(ROOT/'system'/row['name'],pathlib.Path(row['target']),row['mode'])
            for row in MANIFEST['remove']:tx.remove(pathlib.Path(row['target']))
            run(['systemctl','daemon-reload']);run(['udevadm','control','--reload-rules'])
        # The camera daemon and the USB bus remain running throughout deployment.
        run(['systemctl','enable','--now',TIMER])
        run(['systemctl','start','headring-usb-data-host.service','embodied-pi-collect.service','usb-phone-link.service'])
        for _ in range(45):
            try:
                v=snapshot()
                if idle(v):break
            except Exception:pass
            time.sleep(1)
        else:raise RuntimeError('New release did not reach safe idle')
        for n in UNITS+[TIMER]:run(['systemctl','is-active','--quiet',n])
        verify()
        result=dict(device=device,version=MANIFEST['version'],installed=True,state=v['state'],backup=str(tx.backup),files_changed=len(tx.entries))
        (ROOT/'install-result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    except Exception:
        if tx:
            tx.rollback();print(json.dumps(dict(rolled_back=True,backup=str(tx.backup))),flush=True)
        raise

if __name__=='__main__':main()
