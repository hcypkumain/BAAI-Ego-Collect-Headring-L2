#!/usr/bin/python3 -I
"""Narrow sudo helper: no arguments, no ZIP parsing, only local USB volumes."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

if os.geteuid()!=0 or len(sys.argv)!=1:
    raise SystemExit('Root/no-argument invocation required')

ROOT=Path('/run/collector-usb-task-import')
if ROOT.is_symlink():raise SystemExit('Invalid mount directory')
ROOT.mkdir(mode=0o755,exist_ok=True)
if ROOT.stat().st_uid!=0 or ROOT.stat().st_mode & 0o022:raise SystemExit('Unsafe mount directory')
raw=subprocess.check_output(['/usr/bin/lsblk','-J','-o','PATH,TYPE,FSTYPE,LABEL,MOUNTPOINT'],timeout=8)
volumes=[]
def visit(rows):
    for row in rows:
        visit(row.get('children') or [])
        device=row.get('path') or '';name=Path(device).name
        if not re.fullmatch(r'sd[a-z]+[0-9]*',name):continue
        sysnode=Path('/sys/class/block')/name
        if not any(re.fullmatch(r'usb\d+',part) for part in sysnode.resolve().parts):continue
        if row.get('type') not in ('disk','part'):continue
        fs=(row.get('fstype') or '').lower()
        if fs not in ('vfat','exfat','ntfs','ntfs3','ext4'):continue
        info=os.stat(device)
        if not stat.S_ISBLK(info.st_mode):continue
        mount=row.get('mountpoint')
        if mount:
            if not mount.startswith(('/media/','/mnt/','/run/media/','/run/collector-usb-task-import/')) or mount=='/media/orangepi/sdcard':continue
            if Path(mount).is_symlink():continue
        else:
            dest=ROOT/('%d-%d'%(os.major(info.st_rdev),os.minor(info.st_rdev)))
            if dest.is_symlink():continue
            dest.mkdir(mode=0o755,exist_ok=True)
            if dest.stat().st_uid!=0 or dest.stat().st_mode & 0o022:continue
            options='ro,nodev,nosuid,noexec'
            if fs in ('vfat','exfat','ntfs','ntfs3'):options+=',uid=1000,gid=1000,umask=022'
            if fs=='ext4':options+=',noload'
            r=subprocess.run(['/bin/mount','-t',fs,'-o',options,device,str(dest)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=12)
            if r.returncode:continue
            mount=str(dest)
        if not Path(mount).is_dir():continue
        volumes.append({'device':device,'mountpoint':mount,'label':str(row.get('label') or name)[:128]})
        if len(volumes)>=8:return
visit(json.loads(raw).get('blockdevices',[]))
print(json.dumps(volumes,ensure_ascii=False))
