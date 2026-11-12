"""Host-wide clock-step exclusion for hardware-owning processes (Linux flock)."""
import fcntl
import os
import stat
import time

LOCK_PATH = '/run/lock/headring-acquisition-time.lock'
_held = []


def acquire(path=LOCK_PATH, timeout=2.0):
    # Root provisions this persistent inode at boot. Never create, truncate,
    # unlink, or follow a symlink: all participants must lock the same inode.
    fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError('invalid acquisition time lock inode')
        if str(path) == LOCK_PATH and (info.st_uid != 0 or info.st_mode & 0o022):
            raise OSError('acquisition time lock must be root-owned and not writable by others')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('clock-step lock held too long; acquisition refused')
                time.sleep(min(0.05, remaining))
        current = os.stat(str(path), follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise OSError('acquisition time lock was replaced')
        _held.append(fd)  # retained until process exit, including exceptions
        return fd
    except BaseException:
        os.close(fd)
        raise
