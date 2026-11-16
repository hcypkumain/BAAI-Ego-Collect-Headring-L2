"""Durable evidence before an exported local recording may enter cleanup.

This is a transaction receipt, not a replacement for a reference ledger entry.
Pending-QC cleanup remains forbidden until its reservation and deferred-QC
workflow can follow exported data without the original local directory.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from cloud_common import CloudError, atomic, check_source, read_json, session_lock
from usb_export_copy import _verify, verify_manifest


def _key(source):
    return hashlib.sha256(str(source).encode()).hexdigest()


def credential_path(base, source, destination):
    if not isinstance(destination,str) or Path(destination).name != destination:raise CloudError('导出目标无效')
    target=hashlib.sha256(destination.encode()).hexdigest()
    return Path(base)/'runtime/usb_exports/credentials'/(_key(source)+'-'+target+'.json')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32*1024*1024:
        raise CloudError('导出台账凭据不存在或不安全')
    try: value = read_json(path)
    except (OSError, ValueError, RecursionError) as error: raise CloudError('导出台账凭据损坏') from error
    if (not isinstance(value, dict) or set(value) != {'payload', 'sha256'}
            or not isinstance(value['payload'], dict) or value['sha256'] != _digest(value['payload'])):
        raise CloudError('导出台账凭据校验失败')
    return value['payload']


def commit_credential(base, store, source, receipt_path, usb):
    source, receipt_path = Path(source), Path(receipt_path)
    root, _ = store.roots()
    if (not source.is_absolute() or source.is_symlink() or source.parent != root or
            not source.is_dir() or not receipt_path.is_absolute() or receipt_path.is_symlink()):
        raise CloudError('导出源或凭据路径无效')
    expected_receipts = Path(base)/'runtime/usb_exports/receipts'
    if receipt_path.parent != expected_receipts or not receipt_path.is_file() or receipt_path.stat().st_size > 32*1024*1024:
        raise CloudError('导出校验记录路径无效')
    with session_lock(base, source.name):
        try: receipt = read_json(receipt_path)
        except (OSError, ValueError, RecursionError) as error: raise CloudError('导出校验记录无效') from error
        if (not isinstance(receipt,dict) or receipt.get('state') != 'verified' or
            receipt.get('source') != str(source) or receipt.get('usb_uuid') != usb.uuid or
            not isinstance(receipt.get('destination'),str) or Path(receipt['destination']).name != receipt['destination']):
            raise CloudError('导出校验记录与源或U盘不匹配')
        transaction_id=receipt.get('transaction_id')
        if (not isinstance(transaction_id,str) or not re.fullmatch(r'[a-f0-9]{32}',transaction_id) or
                receipt_path.name != transaction_id+'.json' or receipt.get('manifest_file') != '.headring-manifest-'+transaction_id+'.json'):
            raise CloudError('U盘事务清单凭据缺失')
        manifest = receipt.get('manifest')
        if not isinstance(manifest,dict): raise CloudError('导出清单无效')
        usb.check(); _verify(usb,receipt['destination'],manifest)
        verify_manifest(usb,source,receipt['destination'],manifest,transaction_id);check_source(source,manifest)
        meta = store.metadata(source)
        if not isinstance(meta,dict) or meta.get('id') != source.name or not isinstance(meta.get('token'),str):
            raise CloudError('采集状态无法核验')
        # Preserve exactly what the existing recording store established, never
        # infer that failed or pending QC takes have passed quality checks.
        from daily_collection import take_snapshot
        snapshot_provider = getattr(store, 'export_snapshot', None)
        snapshot = snapshot_provider(source, meta) if callable(snapshot_provider) else take_snapshot(store, source, metadata=meta)
        reference_hash = None
        reference_path = meta.get('reference_path')
        if reference_path:
            project = Path(store.project).resolve()
            image = (project / reference_path).resolve()
            try: image.relative_to(project / 'configs/environments')
            except ValueError as error: raise CloudError('参考图路径无效') from error
            if image.is_symlink() or not image.is_file(): raise CloudError('参考图不存在')
            reference_hash = hashlib.sha256(image.read_bytes()).hexdigest()
        if meta.get('qualified') is True and not reference_hash:
            raise CloudError('成功批次缺少参考图凭据')
        payload = dict(version=1, source=str(source), usb_uuid=usb.uuid,
            destination=receipt['destination'], transaction_id=transaction_id, manifest=manifest,
            source_token=meta['token'], status=meta.get('status'),
            retention_kind=meta.get('retention_kind'), reference_path=meta.get('reference_path'),
            qualified=meta.get('qualified') is True, qc_state=meta.get('qc_state'),
            reference_state=meta.get('reference_state'), reference_sha256=reference_hash,
            daily_snapshot=snapshot, verification='sha256')
        if payload['qualified'] and payload['status'] != 'success':
            raise CloudError('采集资格与状态不一致')
        target = credential_path(base, source, receipt['destination'])
        if target.is_symlink(): raise CloudError('导出台账路径不安全')
        envelope = {'payload': payload, 'sha256': _digest(payload)}
        if target.exists():
            if _read(target) != payload: raise CloudError('已有不同的导出台账凭据，禁止覆盖')
        else:
            atomic(target,envelope)
        if _read(target) != payload: raise CloudError('导出台账持久校验失败')
        return target


def validate_credential(base, source, manifest, usb_uuid, *, destination, for_cleanup=False):
    source = Path(source)
    value = _read(credential_path(base,source,destination))
    if (value.get('version') != 1 or value.get('source') != str(source) or
        value.get('usb_uuid') != usb_uuid or value.get('destination') != destination or value.get('manifest') != manifest or
        value.get('verification') != 'sha256'):
        raise CloudError('导出台账与事务不匹配')
    if for_cleanup and (value.get('status') == 'pending_qc' or value.get('qc_state') == 'pending'):
        raise CloudError('待质检数据仍需本地文件，暂不能清理')
    return value


def completed_credential(base, source):
    """Return the version referenced by a completed cleanup journal."""
    source = Path(source)
    if not source.is_absolute() or source.exists() or source.is_symlink():
        raise CloudError('源批次尚未清理')
    journal = Path(base)/'runtime/usb_exports/cleanup'/(source.name+'.json')
    if journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 32*1024*1024:
        raise CloudError('清理事务凭据不存在')
    try: operation = read_json(journal)
    except (OSError, ValueError, RecursionError) as error: raise CloudError('清理事务凭据无效') from error
    if not isinstance(operation,dict) or operation.get('state') != 'complete' or operation.get('source') != str(source):
        raise CloudError('清理事务与台账不一致')
    destination=operation.get('destination')
    value=_read(credential_path(base,source,destination))
    manifest=value.get('manifest') or {}
    if operation.get('manifest_digest') != manifest.get('digest'):
        raise CloudError('清理事务与台账不一致')
    return validate_credential(base, source, manifest, value.get('usb_uuid'), destination=destination, for_cleanup=True)
