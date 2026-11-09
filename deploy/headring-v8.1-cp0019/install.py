#!/usr/bin/env python3
"""Install, verify or roll back the cp_0019 v8.1 update, only while idle."""
import fcntl, hashlib, json, os, pathlib, shutil, subprocess, sys, tempfile, urllib.request
HERE=pathlib.Path(__file__).resolve().parent
BASE=pathlib.Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi')
BACKUP=HERE/'backup'
MANIFEST=json.loads((HERE/'manifest.json').read_text())
SERVICES=('embodied-pi-collect.service','usb-phone-link.service','collector-usb-task-mount.timer')

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def validate_manifest():
    rows=MANIFEST['files'];targets=[r['target'] for r in rows]
    allowed_prefix=str(BASE/'src/orangepi_collect')+'/'
    allowed_extra={'/home/orangepi/maintenance/usb-phone-adb/usb_phone_link.py','/usr/local/lib/collector-usb-task-import/mount-readonly',str(BASE/'runtime/fleet-release.json')}
    if len(rows)!=11 or len(set(targets))!=len(rows):raise RuntimeError('invalid manifest')
    for row in rows:
        target=pathlib.Path(row['target']);payload=pathlib.Path(row['payload'])
        if ('..' in target.parts or not (str(target).startswith(allowed_prefix) or str(target) in allowed_extra)
                or payload.is_absolute() or '..' in payload.parts):raise RuntimeError('invalid manifest path')
    return rows

def idle():
    token=(BASE/'runtime/dashboard_token').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:8765/api/display-state',headers={'X-Access-Token':token})
    with urllib.request.urlopen(req,timeout=5) as resp:s=json.load(resp)
    p=s.get('privacy') or {};w=s.get('worker') or {}
    if not (s.get('state') in ('device_confirm','idle','failed_wait','exhausted') and not s.get('session') and not s.get('busy') and w.get('recording_active') is not True and p.get('phase')=='idle' and p.get('required') is False and p.get('stop_confirmed') is True):raise RuntimeError('capture is not confirmed idle')
    for proc in pathlib.Path('/proc').glob('[0-9]*'):
        try:args=(proc/'cmdline').read_bytes().split(b'\0')
        except (OSError,PermissionError):continue
        if any(a.endswith((b'/framework_recorder.py',b'/capture.py')) for a in args):raise RuntimeError('capture worker exists')

def put(source,target):
    old=target.stat();fd,tmp=tempfile.mkstemp(prefix='.headring-v81-',dir=str(target.parent))
    try:
        with os.fdopen(fd,'wb') as f:f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
        os.chmod(tmp,old.st_mode & 0o7777);os.chown(tmp,old.st_uid,old.st_gid);os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('install','rollback','verify'):raise RuntimeError('use install, rollback or verify')
    mode=sys.argv[1];rows=validate_manifest()
    if pathlib.Path('/opt/egorecord/DEVICE_ID').read_text().strip()!='cp_0019':raise RuntimeError('wrong device')
    if mode!='verify' and os.geteuid()!=0:raise RuntimeError('root required')
    for r in rows:
        target=pathlib.Path(r['target']);payload=HERE/r['payload'];backup=BACKUP/r['payload']
        if not target.is_file() or target.is_symlink() or digest(payload)!=r['after']:raise RuntimeError('invalid target or payload: '+str(target))
        if digest(target)!=r['before' if mode=='install' else 'after']:raise RuntimeError('target changed: '+str(target))
        if mode in ('verify','rollback') and (not backup.is_file() or digest(backup)!=r['before']):raise RuntimeError('backup missing or changed: '+str(target))
    if mode=='verify':print('v8.1 payload, deployed files and rollback backup verified:',len(rows));return
    if mode=='install' and BACKUP.exists():raise RuntimeError('backup already exists')
    idle()
    with (BASE/'runtime/capture.lock').open('r') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);idle()
        if mode=='install':
            BACKUP.mkdir()
            for r in rows:
                dest=BACKUP/r['payload'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(r['target'],dest)
        active=[u for u in SERVICES if subprocess.run(['systemctl','is-active','--quiet',u]).returncode==0]
        stopped=[]
        try:
            for unit in reversed(active):subprocess.run(['systemctl','stop',unit],check=True,timeout=45);stopped.append(unit)
            for r in rows:put((HERE if mode=='install' else BACKUP)/r['payload'],pathlib.Path(r['target']))
            for r in rows:
                if digest(pathlib.Path(r['target']))!=r['after' if mode=='install' else 'before']:raise RuntimeError('post-write mismatch')
        except BaseException:
            for r in rows:put((BACKUP if mode=='install' else HERE)/r['payload'],pathlib.Path(r['target']))
            raise
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)
            for unit in reversed(stopped):subprocess.run(['systemctl','start',unit],check=True,timeout=45)
    print(mode+' completed; previously active services restarted; camera daemon untouched')
if __name__=='__main__':main()
