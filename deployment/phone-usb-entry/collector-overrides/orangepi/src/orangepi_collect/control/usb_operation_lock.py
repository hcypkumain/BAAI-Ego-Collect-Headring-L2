"""Cross-process exclusion for USB task reads and export writes."""
import contextlib
import fcntl
import os
import stat
from pathlib import Path

LOCK = Path('/run/collector-usb-task-import/.operation.lock')

@contextlib.contextmanager
def operation_lock(path=LOCK):
    fd=os.open(str(path),os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise ValueError('U盘操作锁不安全')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise ValueError('U盘正用于其他导入或导出') from error
        yield
    finally:os.close(fd)
