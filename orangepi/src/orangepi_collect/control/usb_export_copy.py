"""Copy and verify a recording using only a pinned USB directory descriptor."""
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import shutil
import time
import uuid
from pathlib import Path
from cloud_common import CloudError, atomic, check_source, read_json, session_lock, source_manifest


def _rename_noreplace(fd, old, new):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, 'renameat2', None)
    if fn is None: raise CloudError('系统不支持安全的原子提交')
    result = fn(fd, os.fsencode(old), fd, os.fsencode(new), 1)
    if result:
        number = ctypes.get_errno()
        if number == errno.EEXIST: raise CloudError('U盘已存在同名批次，禁止覆盖')
        raise OSError(number, os.strerror(number))


def _verify(usb, stage, manifest, progress=None):
    expected = {row['path']: row for row in manifest['files']}
    if len(expected) != len(manifest['files']): raise CloudError('清单路径重复')
    found = set(); done = 0
    def walk(relative):
        nonlocal done
        directory = usb.open_dir(stage + ('/' + relative if relative else ''))
        try:
            for name in os.listdir(directory):
                child = relative + '/' + name if relative else name
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if __import__('stat').S_ISDIR(info.st_mode):
                    walk(child); continue
                row = expected.get(child)
                if row is None: raise CloudError('导出目录含未预期文件')
                fd = usb.open_file(stage + '/' + child, os.O_RDONLY)
                try:
                    before = os.fstat(fd); digest = hashlib.sha256()
                    while True:
                        usb.check(); chunk = os.read(fd, 1024 * 1024)
                        if not chunk: break
                        digest.update(chunk); done += len(chunk)
                        if progress: progress('verifying', done, manifest['total_bytes'])
                    after = os.fstat(fd)
                    if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) !=
                        (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) or
                        before.st_size != row['bytes'] or digest.hexdigest() != row['sha256']):
                        raise CloudError('U盘文件校验失败')
                finally: os.close(fd)
                found.add(child)
        finally: os.close(directory)
    walk('')
    if found != set(expected): raise CloudError('U盘文件不完整')
    return dict(files=len(found), bytes=done, method='sha256')


def _clear_partial(usb, stage, manifest, source):
    """Remove only a validated, transaction-owned partial tree through dir FDs."""
    files={row['path']:row for row in manifest['files']};directories=set()
    for name in files:
        path=Path(name)
        for index in range(1,len(path.parts)):
            directories.add('/'.join(path.parts[:index]))
    def inspect(relative):
        directory=usb.open_dir(stage+('/'+relative if relative else ''))
        try:
            for name in os.listdir(directory):
                child=relative+'/'+name if relative else name
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if child not in directories:raise CloudError('导出暂存目录有未预期内容')
                    inspect(child)
                elif stat.S_ISREG(info.st_mode) and child in files:
                    row=files[child]
                    if info.st_size>row['bytes']:raise CloudError('导出暂存文件大小无效')
                    left=usb.open_file(stage+'/'+child,os.O_RDONLY)
                    right=os.open(str(source/child),os.O_RDONLY|os.O_NOFOLLOW)
                    try:
                        while True:
                            usb.check();chunk=os.read(left,1024*1024)
                            if not chunk:break
                            if os.read(right,len(chunk))!=chunk:raise CloudError('导出暂存文件不属于当前批次')
                    finally:os.close(left);os.close(right)
                else:raise CloudError('导出暂存目录有未预期内容')
        finally:os.close(directory)
    def remove(relative):
        directory=usb.open_dir(stage+('/'+relative if relative else ''))
        try:
            for name in os.listdir(directory):
                child=relative+'/'+name if relative else name
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and child in directories:
                    remove(child);usb.check();os.rmdir(name,dir_fd=directory)
                elif stat.S_ISREG(info.st_mode) and child in files:
                    usb.check();os.unlink(name,dir_fd=directory)
                else:raise CloudError('导出暂存内容发生变化')
            os.fsync(directory)
        finally:os.close(directory)
    inspect('');remove('');usb.check()
    root=usb.open_dir()
    try:os.rmdir(stage,dir_fd=root);os.fsync(root)
    finally:os.close(root)


def _manifest_bytes(source, usb, destination, manifest, transaction_id):
    value=dict(version=1,source=str(source),usb_uuid=usb.uuid,destination=destination,
               transaction_id=transaction_id,manifest=manifest)
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def _read_manifest(usb, name):
    fd=usb.open_file(name,os.O_RDONLY)
    try:
        data=bytearray()
        while True:
            usb.check();chunk=os.read(fd,1024*1024)
            if not chunk:break
            data.extend(chunk)
            if len(data)>32*1024*1024:raise CloudError('U盘清单过大')
        return bytes(data)
    finally:os.close(fd)


def verify_manifest(usb, source, destination, manifest, transaction_id):
    name='.headring-manifest-'+transaction_id+'.json'
    if _read_manifest(usb,name)!=_manifest_bytes(source,usb,destination,manifest,transaction_id):
        raise CloudError('U盘清单与导出事务不匹配')
    return name


def _ensure_manifest(usb, source, destination, manifest, transaction_id):
    name='.headring-manifest-'+transaction_id+'.json';partial=name+'.partial'
    expected=_manifest_bytes(source,usb,destination,manifest,transaction_id)
    try:current=_read_manifest(usb,name)
    except FileNotFoundError:current=None
    if current is not None:
        if current!=expected:raise CloudError('U盘已有不同的事务清单')
        return name
    try:fd=usb.open_file(partial,os.O_RDWR|os.O_CREAT|os.O_EXCL)
    except FileExistsError:fd=usb.open_file(partial,os.O_RDWR)
    try:
        existing=bytearray()
        while True:
            usb.check();chunk=os.read(fd,1024*1024)
            if not chunk:break
            existing.extend(chunk)
            if len(existing)>len(expected):raise CloudError('U盘暂存清单无效')
        if not expected.startswith(existing):raise CloudError('U盘暂存清单与事务不匹配')
        view=memoryview(expected)[len(existing):]
        while view:
            usb.check();count=os.write(fd,view)
            if count<=0:raise OSError('U盘清单写入失败')
            view=view[count:]
        os.fsync(fd)
    finally:os.close(fd)
    root=usb.open_dir()
    try:_rename_noreplace(root,partial,name);os.fsync(root);usb.check()
    finally:os.close(root)
    return name


def _save_receipt(path, receipt):
    if path.is_symlink(): raise CloudError('导出凭据路径不安全')
    if path.exists():
        try: old=read_json(path)
        except (OSError,ValueError,RecursionError) as error: raise CloudError('已有导出凭据无效') from error
        keys=('version','state','source','destination','usb_uuid','manifest','verification','local_deleted','manifest_file','transaction_id')
        if not isinstance(old,dict) or any(old.get(key)!=receipt.get(key) for key in keys):
            raise CloudError('事务标识与已有导出凭据冲突')
        return
    atomic(path,receipt)


def copy_recording(base, source, usb, progress=None, *, transaction_id=None):
    source = Path(source)
    if source.is_symlink() or not source.is_dir(): raise CloudError('采集目录无效')
    usb.check()
    if transaction_id is not None and (not isinstance(transaction_id,str) or not re.fullmatch(r'[a-f0-9]{32}', transaction_id)):
        raise CloudError('导出事务标识无效')
    final = source.name + ('-'+transaction_id if transaction_id else '')
    stage = '.headring-export-' + (transaction_id or uuid.uuid4().hex) + '.partial'
    receipt_path = Path(base)/'runtime/usb_exports/receipts'/((transaction_id or uuid.uuid4().hex)+'.json')
    with session_lock(base, source.name):
        manifest = source_manifest(source)
        usb.check()
        if transaction_id is not None:
            try: existing = usb.open_dir(final)
            except FileNotFoundError: existing = None
            if existing is not None:
                os.close(existing)
                verification = _verify(usb,final,manifest,progress)
                manifest_file=verify_manifest(usb,source,final,manifest,transaction_id)
                check_source(source,manifest);usb.check()
                receipt = dict(version=1,state='verified',source=str(source),destination=final,
                    usb_uuid=usb.uuid,manifest=manifest,verification=verification,
                    verified_at=time.time(),local_deleted=False,manifest_file=manifest_file,transaction_id=transaction_id)
                _save_receipt(receipt_path,receipt)
                return receipt_path,receipt
        if transaction_id is not None:
            try: stale = usb.open_dir(stage)
            except FileNotFoundError: stale = None
            if stale is not None:
                os.close(stale);_clear_partial(usb,stage,manifest,source)
        usage = os.fstatvfs(usb.fd)
        if usage.f_bavail * usage.f_frsize < manifest['total_bytes']:
            raise CloudError('U盘剩余空间不足，未开始复制')
        usb.mkdir(stage)
        created = {stage}; copied = 0
        for row in manifest['files']:
            path = Path(row['path']); parent = stage
            for part in path.parts[:-1]:
                parent += '/' + part
                if parent not in created:
                    usb.mkdir(parent); created.add(parent)
            fd = os.open(str(source/path), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                out = usb.open_file(stage+'/'+row['path'], os.O_WRONLY|os.O_CREAT|os.O_EXCL)
                try:
                    while True:
                        usb.check(); chunk = os.read(fd,1024*1024)
                        if not chunk: break
                        view = memoryview(chunk)
                        while view:
                            usb.check(); n = os.write(out, view)
                            if n <= 0: raise OSError('U盘写入失败')
                            view = view[n:]
                        copied += len(chunk)
                        if progress: progress('copying',copied,manifest['total_bytes'])
                    os.fsync(out)
                finally: os.close(out)
            finally: os.close(fd)
        for directory in sorted(created, key=len, reverse=True):
            dfd = usb.open_dir(directory)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        verification = _verify(usb,stage,manifest,progress)
        check_source(source,manifest); usb.check()
        manifest_file=_ensure_manifest(usb,source,final,manifest,transaction_id) if transaction_id else None
        root = usb.open_dir()
        try:
            _rename_noreplace(root, stage, final); os.fsync(root); usb.check()
        finally: os.close(root)
        receipt = dict(version=1,state='verified',source=str(source),destination=final,
                       usb_uuid=usb.uuid,manifest=manifest,verification=verification,
                       verified_at=time.time(),local_deleted=False,manifest_file=manifest_file,transaction_id=transaction_id)
        _save_receipt(receipt_path,receipt)
        return receipt_path,receipt
