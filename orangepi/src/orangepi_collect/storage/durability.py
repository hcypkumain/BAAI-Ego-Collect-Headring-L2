"""Flush completed episode data before publishing a saved status."""
import json,os,stat,tempfile,time
from pathlib import Path

def sync_session(directory):
    root=Path(directory).resolve(strict=True);files=[];directories=[];total=0
    for current,dirs,names in os.walk(str(root),followlinks=False):
        parent=Path(current);directories.append(parent)
        if any((parent/name).is_symlink() for name in dirs):raise OSError('episode contains a directory symlink')
        for name in names:
            if name=='storage_sync.json':continue
            path=parent/name
            fd=os.open(str(path),(os.O_RDWR if os.name=='nt' else os.O_RDONLY)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
            try:
                before=os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):raise OSError('episode contains a non-regular file')
                os.fsync(fd);after=os.fstat(fd)
                if not name.endswith('.log') and (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
                    raise OSError('episode data changed during disk synchronization')
                files.append(path.relative_to(root).as_posix());total+=after.st_size
            finally:os.close(fd)
    if os.name!='nt':
        for directory in reversed(directories):
            fd=os.open(str(directory),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(fd)
            finally:os.close(fd)
    result=dict(version=1,completed=True,completed_at_unix=time.time(),files_synced=len(files),bytes_synced=total,
                method='fsync_files_and_directories',manifest_scope='completed_data_before_terminal_status')
    fd,tmp=tempfile.mkstemp(prefix='.storage-sync-',dir=str(root))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:json.dump(result,handle);handle.flush();os.fsync(handle.fileno())
        os.replace(tmp,str(root/'storage_sync.json'))
        if os.name!='nt':
            fd=os.open(str(root),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(fd)
            finally:os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return result
