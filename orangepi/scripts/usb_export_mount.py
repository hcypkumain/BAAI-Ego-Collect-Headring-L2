#!/usr/bin/python3 -I
"""Mount exactly one local USB filesystem for export; never format a device.

This helper is intended to be installed root-owned and invoked without arguments.
It refuses disks mounted outside the dedicated task-import mount root.
"""
import json
import fcntl
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path('/run/collector-usb-task-import')
SUPPORTED = {'vfat', 'exfat', 'ntfs', 'ntfs3', 'ext4'}


def candidates(rows):
    result = []
    for row in rows:
        result.extend(candidates(row.get('children') or []))
        device = row.get('path') or ''
        name = Path(device).name
        if not re.fullmatch(r'sd[a-z]+[0-9]*', name):
            continue
        if row.get('type') not in ('part', 'disk') or (row.get('fstype') or '').lower() not in SUPPORTED:
            continue
        if not row.get('uuid') or not any(re.fullmatch(r'usb\d+', p) for p in (Path('/sys/class/block') / name).resolve().parts):
            continue
        info = os.stat(device)
        if stat.S_ISBLK(info.st_mode):
            result.append((row, info))
    return result


def safe_directory(path):
    info = path.stat()
    return info.st_uid == 0 and not (info.st_mode & 0o022)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise RuntimeError('需要无参数的受限管理员权限')
    if ROOT.is_symlink():
        raise RuntimeError('挂载目录不安全')
    ROOT.mkdir(mode=0o755, exist_ok=True)
    if not safe_directory(ROOT):
        raise RuntimeError('挂载目录权限不安全')
    operation_fd = os.open(str(ROOT / '.operation.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o666)
    try:
        operation_info = os.fstat(operation_fd)
        if operation_info.st_uid != 0 or not stat.S_ISREG(operation_info.st_mode):
            raise RuntimeError('U盘操作锁不安全')
        os.fchmod(operation_fd, 0o666)
        try: fcntl.flock(operation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise RuntimeError('U盘正用于任务导入或其他导出') from error
    except BaseException:
        os.close(operation_fd); raise
    try:
        fd = os.open(str(ROOT / '.mount.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(lock.fileno())
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise RuntimeError('挂载锁权限不安全')
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('U盘正用于任务导入或其他导出') from error
            mounted = mount_locked()
            print(json.dumps(mounted, ensure_ascii=False), flush=True)
            # Keep the lock until the owner closes the pipe. This also handles a
            # crashed controller, while avoiding a long-lived privileged shell.
            sys.stdin.readline()
            safe_unmount(mounted)
    finally:
        os.close(operation_fd)


def mount_device(target):
    return os.stat(target, follow_symlinks=False).st_dev


def mount_device_count(device):
    expected='%d:%d' % (os.major(device),os.minor(device));count=0
    with open('/proc/self/mountinfo',encoding='utf-8') as stream:
        for line in stream:
            fields=line.split()
            if len(fields)<10 or '-' not in fields:raise RuntimeError('系统挂载信息无效')
            separator=fields.index('-')
            if fields[2]==expected and fields[separator+1]!='autofs':count+=1
    return count


def mount_id(target):
    fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with open('/proc/self/fdinfo/%d' % fd, encoding='ascii') as stream:
            for line in stream:
                if line.startswith('mnt_id:'):return int(line.split(':', 1)[1])
        raise RuntimeError('无法读取U盘挂载编号')
    finally:os.close(fd)


def mount_locked():
    raw = subprocess.check_output(['/usr/bin/lsblk', '-J', '-o', 'PATH,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT'], timeout=8)
    volumes = candidates(json.loads(raw).get('blockdevices', []))
    if len(volumes) != 1:
        raise RuntimeError('请只连接一个带单个受支持分区的U盘')
    row, info = volumes[0]
    device = row['path']; fs = row['fstype'].lower()
    target = ROOT / ('%d-%d' % (os.major(info.st_rdev), os.minor(info.st_rdev)))
    if target.is_symlink():
        raise RuntimeError('挂载路径不安全')
    mounts = [row['mountpoint']] if row.get('mountpoint') else []
    if not mounts:
        target.mkdir(mode=0o755, exist_ok=True)
        if not safe_directory(target):
            raise RuntimeError('挂载路径权限不安全')
    if any(item != str(target) for item in mounts):
        raise RuntimeError('U盘在其他位置挂载，不能自动切换')
    original_mount_id = None
    if mounts:
        raw = subprocess.check_output(['/usr/bin/findmnt', '-J', '-M', str(target), '-o', 'TARGET,SOURCE,UUID,OPTIONS,FSTYPE'], timeout=5)
        layers = json.loads(raw).get('filesystems', [])
        scaffolds = [item for item in layers if item.get('fstype') == 'autofs']
        mounted = [item for item in layers if item.get('fstype') != 'autofs']
        if (len(scaffolds)>1 or any(item.get('source')!='systemd-1' or item.get('target')!=str(target) for item in scaffolds) or len(mounted) != 1 or mounted[0].get('target') != str(target) or
                mounted[0].get('source') != device or
                str(mounted[0].get('uuid','')).upper() != str(row['uuid']).upper() or
                'ro' not in mounted[0].get('options','').split(',') or
                mount_device(target) != info.st_rdev):
            raise RuntimeError('现有只读U盘身份无法核验')
        original_mount_id = mount_id(target)
        if scaffolds:
            # Import's systemd automount has an autofs layer underneath the
            # read-only filesystem. Stop that exact transient mount first;
            # otherwise a later safe eject would leave a live automount.
            stopped=subprocess.run(['/usr/bin/systemd-umount','--no-ask-password',str(target)],capture_output=True,timeout=30)
            if stopped.returncode or mount_device_count(info.st_rdev)!=0:
                raise RuntimeError('无法安全退出只读自动挂载')
            probe=subprocess.run(['/usr/bin/findmnt','-n','-M',str(target)],capture_output=True,timeout=5)
            if probe.returncode==0:
                raise RuntimeError('只读自动挂载仍然存在，拒绝导出')
            mounts=[];original_mount_id=None
    invoking_user = os.environ.get('SUDO_USER','')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*\$?', invoking_user):
        raise RuntimeError('无法确认采集服务用户')
    account = pwd.getpwnam(invoking_user)
    uid = account.pw_uid
    options = 'nodev,nosuid,noexec'
    if fs in ('vfat', 'exfat', 'ntfs', 'ntfs3'):
        options += ',uid=%d,gid=%d,umask=022' % (uid, account.pw_gid)
    if mounts:
        result = subprocess.run(['/bin/mount', '-o', 'remount,rw,' + options, str(target)], capture_output=True, timeout=15)
    else:
        result = subprocess.run(['/bin/mount', '-t', fs, '-o', 'rw,' + options, device, str(target)], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError('无法将U盘挂载为可写')
    raw = subprocess.check_output(['/usr/bin/findmnt', '-J', '-M', str(target), '-o', 'TARGET,SOURCE,UUID,OPTIONS'], timeout=5)
    rows = json.loads(raw).get('filesystems', [])
    if (len(rows) != 1 or rows[0].get('target') != str(target) or rows[0].get('source') != device or str(rows[0].get('uuid', '')).upper() != str(row['uuid']).upper() or 'rw' not in rows[0].get('options', '').split(',') or mount_device(target) != info.st_rdev):
        raise RuntimeError('挂载后U盘身份或写权限不符')
    if mount_device_count(info.st_rdev) != 1:
        raise RuntimeError('U盘存在其他挂载，拒绝导出')
    current_mount_id = mount_id(target)
    if original_mount_id is not None and original_mount_id != current_mount_id:
        raise RuntimeError('切换时U盘挂载身份改变')
    return {'device': device, 'mountpoint': str(target), 'uuid': row['uuid'], 'label': row.get('label') or Path(device).name, 'major': os.major(info.st_rdev), 'minor': os.minor(info.st_rdev), 'mount_id': current_mount_id}


def safe_unmount(mounted):
    raw = subprocess.check_output(['/usr/bin/findmnt', '-J', '-M', mounted['mountpoint'],
                                   '-o', 'TARGET,SOURCE,UUID,OPTIONS'], timeout=5)
    rows = json.loads(raw).get('filesystems', [])
    info = os.stat(mounted['device'])
    if (len(rows) != 1 or rows[0].get('source') != mounted['device'] or
            rows[0].get('target') != mounted['mountpoint'] or
            str(rows[0].get('uuid','')).upper() != str(mounted['uuid']).upper() or
            not stat.S_ISBLK(info.st_mode) or
            (os.major(info.st_rdev), os.minor(info.st_rdev)) != (mounted['major'], mounted['minor']) or
            mount_id(mounted['mountpoint']) != mounted['mount_id'] or mount_device_count(info.st_rdev) != 1):
        raise RuntimeError('U盘身份已改变，拒绝卸载')
    os.sync()
    result = subprocess.run(['/bin/umount', mounted['mountpoint']], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('U盘卸载未完成，请勿拔出')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
