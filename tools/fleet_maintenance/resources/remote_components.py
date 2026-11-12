#!/usr/bin/env python3
"""Root-side USB component transaction, driven by a hash-pinned fleet plan."""
import argparse
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tarfile
import tempfile

FILES = {
    'orangepi/scripts/usb_tasks_mount_readonly.py': ('/usr/local/lib/collector-usb-task-import/mount-readonly', 0o755),
    'deployment/sudoers/collector-usb-task-import': ('/etc/sudoers.d/collector-usb-task-import', 0o440),
    'deployment/systemd/collector-usb-task-mount.service': ('/etc/systemd/system/collector-usb-task-mount.service', 0o644),
    'deployment/systemd/collector-usb-task-mount.timer': ('/etc/systemd/system/collector-usb-task-mount.timer', 0o644),
    'deployment/udev/72-collector-usb-task-import.rules': ('/etc/udev/rules.d/72-collector-usb-task-import.rules', 0o644),
}
SUDOERS = b'orangepi ALL=(root) NOPASSWD: /usr/local/lib/collector-usb-task-import/mount-readonly ""\n'
PIN = 'f5d03b7b5fb197eb76e78610a0bce672e5cc4853'

def sha(data): return hashlib.sha256(data).hexdigest()

def state(path):
    try: st=path.lstat()
    except FileNotFoundError: return {'type':'absent'}
    if not stat.S_ISREG(st.st_mode): raise RuntimeError('unsafe component '+str(path))
    return {'type':'regular','uid':st.st_uid,'gid':st.st_gid,'mode':stat.S_IMODE(st.st_mode),'sha256':sha(path.read_bytes())}

def group_zero_root_only():
    group=grp.getgrgid(0)
    return not any(x.pw_uid != 0 and x.pw_gid == 0 for x in pwd.getpwall()) and not any(pwd.getpwnam(n).pw_uid != 0 for n in group.gr_mem)

def safe_dir(path, *, create=False, root=Path('/'), owner=0, root_group_only=None):
    root=Path(root); path=Path(path); rel=path.relative_to(root)
    if root_group_only is None: root_group_only=group_zero_root_only()
    current=root
    for part in ('',*rel.parts):
        if part: current=current/part
        try: s=current.lstat()
        except FileNotFoundError:
            if not create: raise
            current.mkdir(mode=0o755); os.chmod(current,0o755); s=current.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=owner or s.st_mode&0o002 or (s.st_mode&0o020 and (s.st_gid!=0 or not root_group_only)):
            raise RuntimeError('unsafe component ancestor '+str(current))
    return current

def check_parent(path, root, owner=0, root_group_only=None):
    current=Path(root)
    for part in Path(path).relative_to(root).parts:
        current=current/part
        if not current.exists() and not current.is_symlink(): break
        safe_dir(current,root=root,owner=owner,root_group_only=root_group_only)
    # Existing helper ancestors must be traversable by orangepi; never chmod them.
    helper=Path(root)/'usr/local/lib/collector-usb-task-import'
    if Path(path)==helper and helper.exists() and not (helper.stat().st_mode & 0o001):
        raise RuntimeError('helper directory not traversable; manual review required')

def atomic_write(path, data, mode, uid=0, gid=0, after_replace=None):
    fd,name=tempfile.mkstemp(prefix='.fleet-component-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out: out.write(data); out.flush(); os.fsync(out.fileno())
        if os.geteuid()==0: os.chown(name,uid,gid)
        os.chmod(name,mode); os.replace(name,path)
        if after_replace: after_replace()
        dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(name): os.unlink(name)

def apply_files(root, payload, expected, backup, *, owner=0, group=0, after=None, root_group_only=None):
    """Replace reviewed files, preserving external changes on rollback."""
    root=Path(root); backup=Path(backup); prepared=[]; changed=[]
    for source,(destination,mode) in FILES.items():
        path=root/destination.lstrip('/'); check_parent(path.parent,root,owner,root_group_only); before=state(path)
        wanted={'type':'regular','uid':owner,'gid':group,'mode':mode,'sha256':sha(payload[source])}
        reviewed=expected[destination]
        # An unreadable sudoers file can be skipped only if root proves it already matches.
        if reviewed.get('type')=='unknown' and before==wanted: pass
        elif reviewed!=before: raise RuntimeError('component changed or unreviewable: '+destination)
        prepared.append((source,path,mode,before,wanted))
    if all(before==wanted for _,_,_,before,wanted in prepared): return {'status':'already_current','changed':[]}
    backup.mkdir(mode=0o700)
    record={}
    try:
        for source,path,mode,before,wanted in prepared:
            record[source]=before
            if before['type']=='regular':
                data=path.read_bytes()
                if sha(data)!=before['sha256']: raise RuntimeError('component changed while backing up')
                b=backup/source; b.parent.mkdir(parents=True,exist_ok=True,mode=0o700); b.write_bytes(data); os.chmod(b,0o600)
        (backup/'before.json').write_text(json.dumps(record,sort_keys=True,indent=2)); os.chmod(backup/'before.json',0o600)
        for source,path,mode,before,wanted in prepared:
            if before==wanted: continue
            safe_dir(path.parent,create=True,root=root,owner=owner,root_group_only=root_group_only)
            if snapshot(path)!=before: raise RuntimeError('component drift before install: '+str(path))
            atomic_write(path,payload[source],mode,owner,group,lambda: changed.append((source,path,before,wanted)))
            if after: after(source,path)
        return {'status':'installed','changed':changed,'before':record}
    except Exception as exc:
        errors=rollback_files(changed,backup)
        if errors: raise RuntimeError('rollback_incomplete: '+', '.join(errors)) from exc
        raise

snapshot=state

def rollback_files(changed, backup):
    errors=[]
    for source,path,before,wanted in reversed(changed):
        try:
            if state(path)!=wanted: errors.append(str(path)+' externally changed'); continue
            if before['type']=='absent': path.unlink()
            else:
                data=(Path(backup)/source).read_bytes()
                if sha(data)!=before['sha256']: errors.append(str(path)+' bad backup'); continue
                atomic_write(path,data,before['mode'],before['uid'],before['gid'])
        except Exception as exc: errors.append(str(path)+':'+type(exc).__name__)
    return errors

def run(*args, check=True):
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if check and p.returncode: raise RuntimeError(args[0]+' failed: '+p.stderr.decode(errors='replace')[-500:])
    return p.stdout.decode(errors='replace').strip()

def unit_conflicts():
    for unit in ('collector-usb-task-mount.service','collector-usb-task-mount.timer'):
        for base in ('/etc/systemd/system','/run/systemd/system','/usr/lib/systemd/system','/lib/systemd/system'):
            for directory in (Path(base)/(unit+'.d'),Path(base)/(unit.rsplit('.',1)[1]+'.d')):
                if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or any(directory.glob('*.conf')))):
                    raise RuntimeError('unit/drop-in conflict: '+str(directory))
        out=run('systemctl','show',unit,'-p','LoadState','-p','FragmentPath','-p','DropInPaths',check=False)
        values=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
        if not values.get('LoadState') and not values.get('FragmentPath'): raise RuntimeError('unit state unknown: '+unit)
        if values.get('DropInPaths') or values.get('FragmentPath') not in ('','/etc/systemd/system/'+unit):
            raise RuntimeError('unit/drop-in conflict: '+unit)

def timer_state():
    enabled=run('systemctl','is-enabled','collector-usb-task-mount.timer',check=False)
    active=run('systemctl','is-active','collector-usb-task-mount.timer',check=False)
    if not enabled or not active:
        load=run('systemctl','show','collector-usb-task-mount.timer','-p','LoadState','--value',check=False)
        if load=='not-found': enabled=enabled or 'not-found'; active=active or 'inactive'
    return {'enabled':enabled,'active':active}

def verify_volumes():
    proc=subprocess.run(['/usr/local/lib/collector-usb-task-import/mount-readonly'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if proc.returncode or proc.stderr.strip(): raise RuntimeError('USB helper reported a discovery or mount error')
    volumes=json.loads(proc.stdout)
    if not isinstance(volumes,list): raise RuntimeError('USB helper returned invalid output')
    if not volumes: return {'hardware':'not_attached_or_not_discovered','verified_software_not_hardware':True}
    def inspect():
        return json.loads(run('findmnt','--json','--list','-o','TARGET,SOURCE,MAJ:MIN,FSTYPE,OPTIONS'))['filesystems']
    for volume in volumes:
        device=volume['device']; target=volume['mountpoint']; st=os.stat(device)
        if not stat.S_ISBLK(st.st_mode): raise RuntimeError('USB device is not block device')
        identity='%d:%d'%(os.major(st.st_rdev),os.minor(st.st_rdev))
        def matching(rows):
            all_device=[row for row in rows if row.get('maj:min')==identity and row.get('fstype')!='autofs']
            matches=[row for row in rows if row.get('target')==target and row.get('maj:min')==identity and row.get('fstype')!='autofs']
            if len(matches)!=1 or len(all_device)!=1: raise RuntimeError('USB mount identity is ambiguous')
            options=set((matches[0].get('options') or '').split(','))
            if not {'ro','nodev','nosuid','noexec'}.issubset(options) or 'rw' in options: raise RuntimeError('USB mount verification failed')
            visible=os.stat(target).st_dev
            if '%d:%d'%(os.major(visible),os.minor(visible))!=identity: raise RuntimeError('USB visible identity mismatch')
            return matches[0]
        if matching(inspect())!=matching(inspect()): raise RuntimeError('USB mount changed during verification')
    return {'hardware':'mount_verified_readonly','verified_software_not_hardware':False,'volumes':volumes}

def package(path, expected_manifest):
    with tarfile.open(path,'r:') as tar:
        members=tar.getmembers(); names={m.name for m in members}
        if len(members)!=6 or names!={'manifest.json',*('payload/'+x for x in FILES)} or any(not x.isfile() for x in members):
            raise RuntimeError('invalid component archive')
        raw=tar.extractfile('manifest.json').read()
        if sha(raw)!=expected_manifest: raise RuntimeError('component manifest hash mismatch')
        manifest=json.loads(raw); payload={x:tar.extractfile('payload/'+x).read() for x in FILES}
    if manifest.get('commit')!=PIN or set(manifest.get('files',{}))!=set(FILES): raise RuntimeError('component manifest pin/paths mismatch')
    for key,(dest,mode) in FILES.items():
        item=manifest['files'][key]
        if item.get('destination')!=dest or item.get('mode')!=mode or item.get('size')!=len(payload[key]) or item.get('sha256')!=sha(payload[key]): raise RuntimeError('component manifest content mismatch')
    if payload['deployment/sudoers/collector-usb-task-import']!=SUDOERS: raise RuntimeError('sudoers rule not exact')
    return payload

def module():
    path=Path(__file__).with_name('remote_transaction.py')
    spec=importlib.util.spec_from_file_location('remote_transaction',path); rt=importlib.util.module_from_spec(spec); spec.loader.exec_module(rt)
    return rt

def main():
    p=argparse.ArgumentParser(); p.add_argument('--device',required=True); p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--plan',type=Path,required=True); p.add_argument('--plan-sha256',required=True); p.add_argument('--archive-sha256',required=True)
    p.add_argument('--manifest-sha256',required=True); p.add_argument('--apply',action='store_true'); p.add_argument('--verify',action='store_true'); args=p.parse_args()
    if args.apply and args.verify: raise RuntimeError('choose apply or verify')
    if os.geteuid()!=0: raise RuntimeError('root required')
    os.umask(0o077); rt=module(); rt.TARGET_DEVICE=args.device; rt.require_device()
    for path in (Path(__file__).absolute(),Path(rt.__file__).absolute(),args.archive,args.plan): rt.require_root_private_tree(path)
    if rt.digest(args.plan)!=args.plan_sha256 or rt.digest(args.archive)!=args.archive_sha256: raise RuntimeError('input hash mismatch')
    plan=json.loads(args.plan.read_text()); payload=package(args.archive,args.manifest_sha256)
    if (plan.get('device')!=args.device or plan.get('commit')!=PIN or plan.get('component_manifest_sha256')!=args.manifest_sha256
        or plan.get('component_bundle_sha256')!=args.archive_sha256 or plan.get('component_tool_sha256')!=rt.digest(Path(__file__))): raise RuntimeError('plan does not pin component input')
    expected={x['path']:x['before'] for x in plan['component_diff']}
    if set(expected)!={dest for dest,_ in FILES.values()}: raise RuntimeError('component plan incomplete')
    rt.assert_idle(rt.api_state()); unit_conflicts()
    if not rt.service_active('embodied-pi-collect') or not rt.service_active('camd'): raise RuntimeError('required service inactive')
    protected=rt.protected_snapshot()
    if protected!=plan['protected'] or rt.camd_pid()!=plan['camd_pid']: raise RuntimeError('protected state or camd drift')
    root=Path('/'); statuses={}
    for source,(dest,mode) in FILES.items():
        check_parent(Path(dest).parent,root)
        actual=state(Path(dest)); wanted={'type':'regular','uid':0,'gid':0,'mode':mode,'sha256':sha(payload[source])}
        prior=expected[dest]
        if not args.verify and prior!=actual and not (prior.get('type')=='unknown' and actual==wanted): raise RuntimeError('component baseline drift: '+dest)
        statuses[dest]='same' if actual==wanted else 'different'
    timer=timer_state()
    if timer['enabled'] not in ('enabled','disabled','not-found') or timer['active'] not in ('active','inactive'):
        raise RuntimeError('timer state unknown; manual review required')
    reviewed_timer=plan.get('component_timer') or {}
    if not args.verify and timer!={'enabled':reviewed_timer.get('is-enabled'),'active':reviewed_timer.get('is-active')}:
        raise RuntimeError('timer state changed since plan')
    if args.verify:
        if any(x!='same' for x in statuses.values()) or timer!={'enabled':'enabled','active':'active'}: raise RuntimeError('component or timer verification failed')
        print(json.dumps({'status':'components_verified','hardware':'not_checked'})); return
    if not args.apply:
        print(json.dumps({'status':'dry_run_passed','components':statuses,'timer':timer})); return
    if all(x=='same' for x in statuses.values()) and timer=={'enabled':'enabled','active':'active'}:
        print(json.dumps({'status':'already_current','hardware':'not_checked'})); return
    input_root=args.plan.parent; backup=input_root/'component-backup'; receipt=input_root/'component-receipt.json'
    pids={name:run('systemctl','show','-p','MainPID','--value',name) for name in ('embodied-pi-collect','camd')}
    sd=run('findmnt','-n','-o','SOURCE','--mountpoint','/media/orangepi/sdcard')
    changed=[]; result=None; timer_changed=False
    with rt.hold_capture_locks():
        try:
            rt.assert_idle(rt.api_state()); unit_conflicts()
            candidate=input_root/'sudoers-candidate'; candidate.write_bytes(payload['deployment/sudoers/collector-usb-task-import']); os.chmod(candidate,0o440)
            run('visudo','-c','-f',str(candidate))
            result=apply_files(root,payload,expected,backup); changed=result.get('changed',[])
            if changed:
                run('visudo','-c')
                run('systemd-analyze','verify','/etc/systemd/system/collector-usb-task-mount.service','/etc/systemd/system/collector-usb-task-mount.timer')
                run('systemctl','daemon-reload'); run('udevadm','control','--reload-rules')
            if timer['enabled']!='enabled' or timer['active']!='active':
                timer_changed=True; run('systemctl','enable','--now','collector-usb-task-mount.timer')
            if changed: run('systemctl','start','collector-usb-task-mount.service')
            usb=verify_volumes()
            rt.assert_idle(rt.api_state())
            if rt.protected_snapshot()!=protected or any(run('systemctl','show','-p','MainPID','--value',n)!=pid for n,pid in pids.items()): raise RuntimeError('protected or service state changed')
            if run('findmnt','-n','-o','SOURCE','--mountpoint','/media/orangepi/sdcard')!=sd: raise RuntimeError('SD source changed')
            if timer_state()!={'enabled':'enabled','active':'active'}: raise RuntimeError('timer verification failed')
            for source,(dest,mode) in FILES.items():
                if state(Path(dest))!={'type':'regular','uid':0,'gid':0,'mode':mode,'sha256':sha(payload[source])}: raise RuntimeError('component post-install hash/mode mismatch: '+dest)
            report={'status':'verified','changed':len(changed),'usb':usb,'note':'read-only mounts, if any, were not removed'}
            receipt.write_text(json.dumps(report,sort_keys=True)); os.chmod(receipt,0o600); print(json.dumps(report))
        except Exception as exc:
            problems=rollback_files(changed,backup) if changed else []
            if 'rollback_incomplete' in str(exc): problems.append(str(exc))
            if timer_changed:
                try:
                    if timer['active']!='active': run('systemctl','stop','collector-usb-task-mount.timer')
                    if timer['enabled']!='enabled': run('systemctl','disable','collector-usb-task-mount.timer')
                except Exception as error: problems.append('timer restore: '+str(error))
            if changed:
                try: run('systemctl','daemon-reload'); run('udevadm','control','--reload-rules')
                except Exception as error: problems.append('reload: '+str(error))
            receipt.write_text(json.dumps({'status':'rollback_incomplete' if problems else 'rolled_back','error':str(exc),'problems':problems,'mounts':'not_unmounted'},sort_keys=True)); os.chmod(receipt,0o600)
            raise

if __name__=='__main__': main()
