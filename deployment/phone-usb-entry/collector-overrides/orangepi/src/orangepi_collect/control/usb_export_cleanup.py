"""Prototype cleanup transaction; caller must preserve ledger and capture state.

Not wired into the collector: integration requires independently reviewed
ledger, mount-identity and capture-state guarantees.
"""
import os
import shutil
import stat
from pathlib import Path
from cloud_common import CloudError, atomic, check_source, read_json, session_lock, sync_dir
from usb_export_copy import _verify, verify_manifest
from usb_export_ledger import validate_credential


def _check_partial_tree(target, manifest):
    expected = {row['path'] for row in manifest['files']}
    for current, dirs, files in os.walk(target, followlinks=False):
        parent = Path(current)
        for name in dirs:
            path = parent / name
            if path.is_symlink() or not path.is_dir():
                raise CloudError('清理暂存目录包含不安全路径')
        for name in files:
            path = parent / name
            if path.relative_to(target).as_posix() not in expected or not stat.S_ISREG(path.lstat().st_mode):
                raise CloudError('清理暂存目录包含未预期文件')


def _purge(target, manifest, usb):
    # Recheck the mount before every irreversible unlink; a disconnect leaves a
    # journaled partial tree that can resume only after the same disk returns.
    _check_partial_tree(target, manifest)
    for current, dirs, files in os.walk(target, topdown=False, followlinks=False):
        parent=Path(current)
        for name in files:
            usb.check();path=parent/name
            if not stat.S_ISREG(path.lstat().st_mode):raise CloudError('清理目录内容已变化')
            path.unlink()
        for name in dirs:
            usb.check();path=parent/name
            if path.is_symlink() or not path.is_dir():raise CloudError('清理目录内容已变化')
            path.rmdir()
        sync_dir(parent)
    usb.check();target.rmdir()


def cleanup(base, source, destination, manifest, usb):
    source = Path(source)
    if not source.is_absolute() or source.is_symlink() or not isinstance(destination,str) or Path(destination).name != destination:
        raise CloudError('清理路径无效')
    credential = validate_credential(base, source, manifest, usb.uuid, destination=destination, for_cleanup=True)
    if credential.get('destination') != destination:
        raise CloudError('导出目标与台账凭据不符')
    trash_root = source.parent / '.usb-export-cleanup'
    journal = Path(base) / 'runtime/usb_exports/cleanup' / (source.name + '.json')
    with session_lock(base, source.name):
        usb.check()
        value = read_json(journal)
        target = trash_root / source.name
        if value is None:
            if not source.is_dir() or target.exists():
                raise CloudError('源目录不存在或存在未恢复的清理事务')
            _verify(usb, destination, manifest)
            verify_manifest(usb,source,destination,manifest,credential['transaction_id'])
            check_source(source, manifest)
            trash_root.mkdir(exist_ok=True)
            if trash_root.is_symlink() or trash_root.stat().st_dev != source.stat().st_dev:
                raise CloudError('清理临时目录不安全')
            value = {'source': str(source), 'destination': str(destination), 'target': str(target),
                     'manifest_digest': manifest['digest'], 'state': 'prepared'}
            atomic(journal, value)
        if (value.get('source') != str(source) or value.get('destination') != str(destination)
                or value.get('target') != str(target) or value.get('manifest_digest') != manifest['digest']):
            raise CloudError('清理事务与当前请求不一致')
        if value.get('state') == 'complete':
            if source.exists() or target.exists():
                raise CloudError('已完成的清理事务出现源目录或暂存目录冲突')
            return value
        usb.check()
        _verify(usb, destination, manifest)
        verify_manifest(usb,source,destination,manifest,credential['transaction_id'])
        if source.exists():
            if target.exists() or value.get('state') != 'prepared':
                raise CloudError('清理事务冲突')
            check_source(source, manifest)
            os.rename(source, target)
            sync_dir(source.parent); sync_dir(trash_root)
        if target.exists():
            if target.is_symlink() or value.get('state') not in ('prepared', 'purging'):
                raise CloudError('临时目录身份无效')
            # A prior interrupted purge cannot pass a full source check. Refuse
            # automatic continuation instead of assuming a partial tree is safe.
            if value.get('state') == 'prepared':
                check_source(target, manifest)
                value['state'] = 'purging'; atomic(journal, value)
            else:
                _check_partial_tree(target, manifest)
            usb.check()
            _purge(target, manifest, usb); sync_dir(trash_root)
        elif value.get('state') != 'purging':
            raise CloudError('清理目标缺失，需要人工检查')
        value['state'] = 'complete'; atomic(journal, value)
        return value
