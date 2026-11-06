"""Durable upload records and immutable source manifests; no network access."""
import contextlib,fcntl,hashlib,json,os,stat,time,uuid
from pathlib import Path

class CloudError(ValueError):pass

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        fd=os.open(str(temp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,allow_nan=False);stream.flush();os.fsync(stream.fileno())
        os.replace(str(temp),str(path));sync_dir(path.parent)
    finally:
        if temp.exists():temp.unlink()

def sync_dir(path):
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)

def read_json(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default

@contextlib.contextmanager
def session_lock(base,session):
    if not isinstance(session,str) or not session or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._' for c in session):
        raise CloudError('采集批次标识无效')
    directory=Path(base)/'runtime/cloud_uploads/session_locks';directory.mkdir(parents=True,exist_ok=True)
    with (directory/(session+'.lock')).open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CloudError('本批次正在上传或清理，请等待完成后操作')
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)

def source_manifest(source,hashes=True,progress=None):
    source=Path(source)
    if source.is_symlink() or not source.is_dir():raise CloudError('本地采集目录不存在或已变化')
    rows=[];total=0
    for current,dirs,names in os.walk(str(source),followlinks=False):
        parent=Path(current)
        if any((parent/name).is_symlink() for name in dirs):raise CloudError('采集目录中含链接，已停止上传')
        for name in sorted(names):
            path=parent/name
            if name.endswith(('.tmp','.partial','.active')):raise CloudError('批次包含尚未完成的文件')
            fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
            try:
                before=os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):raise CloudError('批次包含非普通文件')
                digest=hashlib.sha256()
                if hashes:
                    while True:
                        block=os.read(fd,1024*1024)
                        if not block:break
                        digest.update(block)
                after=os.fstat(fd)
                if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                    raise CloudError('本地文件在校验期间发生变化，已停止上传')
                row=dict(path=path.relative_to(source).as_posix(),bytes=before.st_size,mtime_ns=before.st_mtime_ns,inode=before.st_ino)
                if hashes:row['sha256']=digest.hexdigest()
                rows.append(row);total+=before.st_size
                if progress:progress(len(rows),total)
            finally:os.close(fd)
    rows.sort(key=lambda row:row['path'])
    if not rows:raise CloudError('批次目录为空')
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(files=rows,total_files=len(rows),total_bytes=total,digest=digest)

def same_manifest(expected,actual,hashes=True):
    # exFAT inode numbers change after remount; hashes still prove identical data.
    # source_manifest retains before/after inode checks while hashing an open file.
    fields=('path','bytes','mtime_ns','sha256') if hashes else ('path','bytes','mtime_ns')
    return len(expected['files'])==len(actual['files']) and all(
        all(left.get(key)==right.get(key) for key in fields) for left,right in zip(expected['files'],actual['files']))

def check_source(source,manifest):
    if not same_manifest(manifest,source_manifest(source)):raise CloudError('本地批次内容与上传清单不同，禁止覆盖或删除')

def verify_remote(manifest,prefix,objects):
    expected={prefix+row['path']:row['bytes'] for row in manifest['files']}
    actual={obj.name:int(obj.size) for obj in objects}
    if expected!=actual:raise CloudError('云端对象名称、数量或逐文件大小不完整，暂不可删除本地数据')
    return dict(files=len(actual),bytes=sum(actual.values()),method='all_remote_object_names_and_per_file_sizes')
