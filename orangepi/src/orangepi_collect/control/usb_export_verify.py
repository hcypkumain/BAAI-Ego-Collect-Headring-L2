"""Compare an exported recording's bytes with its immutable source manifest.

This module only verifies data; it never mounts, copies, or removes files.
"""
import hashlib
import os
import stat
from pathlib import Path

from cloud_common import CloudError


def verify_export(destination, manifest):
    root = Path(destination)
    if root.is_symlink() or not root.is_dir():
        raise CloudError('U盘目标目录不存在或为链接')
    expected = {row['path']: row for row in manifest['files']}
    if len(expected) != len(manifest['files']):
        raise CloudError('源清单包含重复路径')
    found = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in dirs:
            if (parent / name).is_symlink():
                raise CloudError('导出目录包含链接')
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            row = expected.get(relative)
            if row is None:
                raise CloudError('导出目录包含额外文件')
            fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size != row['bytes']:
                    raise CloudError('导出文件类型或大小不符')
                digest = hashlib.sha256()
                while True:
                    block = os.read(fd, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                after = os.fstat(fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise CloudError('校验期间导出文件发生变化')
                if digest.hexdigest() != row['sha256']:
                    raise CloudError('导出文件内容校验失败')
            finally:
                os.close(fd)
            found.add(relative)
    if found != set(expected):
        raise CloudError('U盘缺少导出文件')
    return {'files': len(found), 'bytes': sum(row['bytes'] for row in expected.values()), 'method': 'sha256'}
