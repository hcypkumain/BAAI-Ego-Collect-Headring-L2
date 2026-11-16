#!/usr/bin/python3 -I
"""No-argument root helper for read-only, device-bound USB task volumes."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import fcntl

ROOT=Path('/run/collector-usb-task-import')

def mount_command(device,dest,fs):
    options='ro,nodev,nosuid,noexec'
    if fs in ('vfat','exfat','ntfs','ntfs3'):options+=',uid=1000,gid=1000,umask=022'
    if fs=='ext4':options+=',noload'
    return ['/usr/bin/systemd-mount','--no-ask-password','--quiet','--collect',
            '--bind-device','--automount=yes','--timeout-idle-sec=0','--fsck=no',
            '-t',fs,'-o',options,device,str(dest)]

def eligible(row,sysroot=Path('/sys/class/block')):
    device=row.get('path') or '';name=Path(device).name
    if not re.fullmatch(r'/dev/sd[a-z]+[0-9]*',device):return False
    if row.get('type') not in ('disk','part'):return False
    if (row.get('fstype') or '').lower() not in ('vfat','exfat','ntfs','ntfs3','ext4'):return False
    if not any(re.fullmatch(r'usb\d+',p) for p in (sysroot/name).resolve().parts):return False
    mount=row.get('mountpoint')
    if mount and (not mount.startswith(('/media/','/mnt/','/run/media/',str(ROOT)+'/')) or mount=='/media/orangepi/sdcard'):
        return False
    return True

def mounted_readonly(device, mount, info, errors=None):
    """Verify the visible read-only filesystem and optional systemd scaffold."""
    try:
        target=str(Path(mount))
        if not target.startswith('/') or target != mount:return False
        identity='%d:%d'%(os.major(info.st_rdev),os.minor(info.st_rdev))
        def snapshot():
            raw=subprocess.check_output(['/usr/bin/findmnt','--json','--list','--output',
                 'ID,TARGET,SOURCE,MAJ:MIN,FSTYPE,OPTIONS'], timeout=8)
            rows=json.loads(raw).get('filesystems',[])
            unique={}
            for row in rows:
                mount_id=row.get('id')
                if mount_id is None:return None
                if mount_id in unique and unique[mount_id]!=row:return None
                unique[mount_id]=row
            rows=list(unique.values())
            device_rows=[row for row in rows if row.get('fstype')!='autofs' and row.get('maj:min')==identity]
            target_rows=[row for row in rows if row.get('target')==target]
            if len(device_rows)!=1:
                if errors is not None:
                    writable=[r.get('target') for r in device_rows
                              if 'rw' in (r.get('options') or '').split(',')]
                    if writable:errors.append(dict(device=device,stage='verify',error='same device mounted read-write',mountpoints=writable))
                return None
            row=device_rows[0]
            real=[entry for entry in target_rows if entry.get('fstype')!='autofs']
            scaffolds=[entry for entry in target_rows if entry.get('fstype')=='autofs']
            if len(real)!=1 or real[0]!=row or len(scaffolds)>1:return None
            if scaffolds and scaffolds[0].get('source')!='systemd-1':return None
            options=set((row.get('options') or '').split(','))
            if not {'ro','nodev','nosuid','noexec'}.issubset(options) or 'rw' in options:return None
            visible=os.stat(target).st_dev
            if '%d:%d'%(os.major(visible),os.minor(visible))!=identity:return None
            return row, scaffolds
        first=snapshot()
        return first is not None and snapshot()==first
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def visit(rows,volumes,errors=None):
    errors = [] if errors is None else errors
    for row in rows:
        if len(volumes)>=8:return
        visit(row.get('children') or [],volumes,errors)
        if not eligible(row):continue
        device=row['path'];fs=row['fstype'].lower()
        try:
            info=os.stat(device)
            if not stat.S_ISBLK(info.st_mode):continue
            mount=row.get('mountpoint')
            if mount:
                if Path(mount).is_symlink():continue
            else:
                dest=ROOT/('%d-%d'%(os.major(info.st_rdev),os.minor(info.st_rdev)))
                if dest.is_symlink():continue
                dest.mkdir(mode=0o755,exist_ok=True)
                if dest.stat().st_uid!=0 or dest.stat().st_mode & 0o022:continue
                result=subprocess.run(mount_command(device,dest,fs),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
                if result.returncode:
                    errors.append(dict(device=device,stage='mount',error=result.stderr.decode(errors='replace')[-600:]));continue
                mount=str(dest)
                # Resolve the device-bound autofs trigger before validating the
                # actual filesystem; an untriggered scaffold is not a volume.
                with os.scandir(dest):pass
            if Path(mount).is_dir() and mounted_readonly(device,mount,info,errors):
                volumes.append({'device':device,'mountpoint':mount,'label':str(row.get('label') or Path(device).name)[:128]})
        except (OSError,subprocess.TimeoutExpired) as exc:
            # Unplugging one disk must not prevent other disks from being found.
            errors.append(dict(device=device,stage='discover',error=str(exc)[:600]))


def main():
    if os.geteuid()!=0 or len(sys.argv)!=1:
        raise SystemExit('Root/no-argument invocation required')
    if ROOT.is_symlink():raise SystemExit('Invalid mount directory')
    ROOT.mkdir(mode=0o755,exist_ok=True)
    if ROOT.stat().st_uid!=0 or ROOT.stat().st_mode & 0o022:
        raise SystemExit('Unsafe mount directory')
    operation_fd = os.open(str(ROOT / '.operation.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o666)
    try:
        operation_info = os.fstat(operation_fd)
        if operation_info.st_uid != 0 or not stat.S_ISREG(operation_info.st_mode):
            raise RuntimeError('U盘操作锁不安全')
        os.fchmod(operation_fd, 0o666)
    finally:
        os.close(operation_fd)
    fd=os.open(str(ROOT/'.mount.lock'),os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'r+') as lock:
        if os.fstat(lock.fileno()).st_uid!=0:raise SystemExit('Unsafe mount lock')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            # A hotplug scan already owns the helper. Report mounted volumes
            # without starting a second mount or queuing behind a slow device.
            raw=subprocess.check_output(['/usr/bin/lsblk','-J','-o','PATH,TYPE,FSTYPE,LABEL,MOUNTPOINT'],timeout=8)
            volumes=[]
            def existing(rows):
                for row in rows:
                    existing(row.get('children') or [])
                    mount=row.get('mountpoint')
                    try:
                        if eligible(row) and mount and not Path(mount).is_symlink() and Path(mount).is_dir() and stat.S_ISBLK((info:=os.stat(row['path'])).st_mode) and mounted_readonly(row['path'],mount,info):
                            volumes.append(dict(device=row['path'],mountpoint=mount,label=str(row.get('label') or Path(row['path']).name)[:128]))
                    except OSError:continue
            existing(json.loads(raw).get('blockdevices',[]))
            print(json.dumps(volumes[:8],ensure_ascii=False));return
        raw=subprocess.check_output(['/usr/bin/lsblk','-J','-o','PATH,TYPE,FSTYPE,LABEL,MOUNTPOINT'],timeout=8)
        volumes=[];errors=[];visit(json.loads(raw).get('blockdevices',[]),volumes,errors)
    print(json.dumps(volumes,ensure_ascii=False))
    if errors:print(json.dumps({'mount_errors':errors},ensure_ascii=False),file=sys.stderr)

if __name__=='__main__':main()
