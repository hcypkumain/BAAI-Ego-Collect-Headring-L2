"""Pin an export volume with an open directory FD and check mount identity.

All target files must be opened relative to this FD. A successful check alone
is not a substitute for openat: the mount can disappear immediately afterward.
"""
import os
import re
import stat
import subprocess
from pathlib import Path
from cloud_common import CloudError


def _mount_id(fd):
    with open('/proc/self/fdinfo/%d' % fd, encoding='ascii') as stream:
        for line in stream:
            if line.startswith('mnt_id:'):
                return int(line.split(':', 1)[1])
    raise CloudError('无法读取U盘挂载编号')


def _mounts():
    result = {}
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 10 or '-' not in fields:
                raise CloudError('系统挂载信息无效')
            mount_id = int(fields[0]); major, minor = map(int, fields[2].split(':'))
            target = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), fields[4])
            result[mount_id] = (os.makedev(major, minor), target, fields[5].split(','))
    return result


def _uuid(device):
    try:
        return subprocess.check_output(['/usr/sbin/blkid', '-s', 'UUID', '-o', 'value',
                                        '/dev/block/%d:%d' % (os.major(device), os.minor(device))],
                                       stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise CloudError('无法核验U盘UUID') from error


class UsbIdentity:
    def __init__(self, mountpoint, uuid):
        self.path = Path(mountpoint)
        if not self.path.is_absolute() or self.path.is_symlink() or not isinstance(uuid, str) or not uuid:
            raise CloudError('U盘身份无效')
        self.uuid = uuid
        self.fd = os.open(str(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            self.device, self.inode, self.mount_id = info.st_dev, info.st_ino, _mount_id(self.fd)
            self.check()
        except BaseException:
            os.close(self.fd); self.fd = None
            raise

    def check(self):
        if self.fd is None: raise CloudError('U盘句柄已关闭')
        try:
            info = os.fstat(self.fd)
            current = os.stat(str(self.path), follow_symlinks=False)
            mounts = _mounts(); entry = mounts.get(self.mount_id)
            same_device = [key for key, value in mounts.items() if value[0] == self.device]
            if (not stat.S_ISDIR(current.st_mode) or
                (info.st_dev, info.st_ino) != (self.device, self.inode) or
                (current.st_dev, current.st_ino) != (self.device, self.inode) or
                _mount_id(self.fd) != self.mount_id or entry is None or same_device != [self.mount_id] or
                entry[0] != self.device or entry[1] != str(self.path) or 'rw' not in entry[2] or
                _uuid(self.device).upper() != self.uuid.upper()):
                raise CloudError('U盘已断开、重新挂载或身份不匹配')
        except OSError as error:
            raise CloudError('U盘已断开或无法访问') from error

    def open_file(self, relative, flags, mode=0o600):
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in ('', '.', '..') for p in parts):
            raise CloudError('U盘目标路径无效')
        self.check()
        parent = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent); parent = next_fd
                if _mount_id(parent) != self.mount_id:
                    raise CloudError('U盘目录包含其他挂载点')
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                self.check()
                if (_mount_id(fd) != self.mount_id or os.fstat(fd).st_dev != self.device
                        or not stat.S_ISREG(os.fstat(fd).st_mode)):
                    raise CloudError('U盘目标不是同一文件系统上的普通文件')
                return fd
            except BaseException:
                os.close(fd); raise
        finally:
            os.close(parent)

    def open_dir(self, relative=''):
        self.check()
        parts = Path(relative).parts if relative else ()
        if Path(relative).is_absolute() or any(p in ('', '.', '..') for p in parts):
            raise CloudError('U盘目录路径无效')
        fd = os.dup(self.fd)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
                if _mount_id(fd) != self.mount_id:
                    raise CloudError('U盘目录包含其他挂载点')
            self.check()
            return fd
        except BaseException:
            os.close(fd); raise

    def mkdir(self, relative):
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in ('', '.', '..') for p in parts):
            raise CloudError('U盘目录路径无效')
        parent = self.open_dir('/'.join(parts[:-1]))
        try:
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
            os.fsync(parent)
            self.check()
        finally:
            os.close(parent)

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
