"""Durable exclusive admission for scanner and camera-owning processes.

Registry lock protects state only: never hold it while waiting for a process.
Unknown ownership is fail closed; recovery never signals historical process IDs.
"""
import contextlib
import hashlib
import hmac
import uuid
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import tempfile
import time


WRAPPER_PATH=Path(__file__).with_name('camera_owner_wrapper.py')


class RegistryError(RuntimeError):pass


def valid_boot(value):
    try:return isinstance(value,str) and str(uuid.UUID(value))==value
    except (ValueError,AttributeError,TypeError):return False


def boot_id():
    try:
        value=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return value if valid_boot(value) else None
    except OSError:return None


def checksum(value):
    encoded=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def proc_identity(pid):
    try:
        raw=Path('/proc/%d/stat'%pid).read_text()
        values=raw[raw.rindex(')')+2:].split()
        return {'state':values[0], 'pgrp':int(values[2]), 'sid':int(values[3]), 'start':int(values[19])}
    except FileNotFoundError:return None
    except (OSError,ValueError,IndexError):raise RegistryError('无法核对进程身份')


def group_exists(pgid):
    try:os.killpg(pgid,0);return True
    except ProcessLookupError:return False
    except OSError:raise RegistryError('无法核对扫描进程组')


class Registry:
    def __init__(self,root):
        self.root=Path(root);self.record=self.root/'camera_owner.json';self.witness=self.root/'active_run_witness.json';self.lockfile=self.root/'camera_owner.lock'
    @contextlib.contextmanager
    def locked(self):
        self.root.mkdir(parents=True,exist_ok=True)
        with self.lockfile.open('a+') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            try:
                opened=os.fstat(f.fileno());current=self.lockfile.stat()
                if (opened.st_dev,opened.st_ino)!=(current.st_dev,current.st_ino):
                    raise RegistryError('设备所有权锁文件已变化，需要维护恢复')
                yield
            finally:fcntl.flock(f,fcntl.LOCK_UN)
    def _read(self):
        try:value=json.loads(self.record.read_text())
        except FileNotFoundError:return None
        except (OSError,ValueError,TypeError):raise RegistryError('设备所有权记录损坏，需要维护恢复')
        if not isinstance(value,dict):raise RegistryError('设备所有权记录损坏，需要维护恢复')
        digest=value.pop('integrity_sha256',None)
        try:valid=isinstance(digest,str) and hmac.compare_digest(digest,checksum(value))
        except (TypeError,ValueError):valid=False
        if not valid:raise RegistryError('设备所有权记录校验失败，需要维护恢复')
        if (value.get('schema')!=1 or not isinstance(value.get('run'),str)
            or not isinstance(value.get('nonce'),str) or not isinstance(value.get('witness_sha256'),str)
            or not valid_boot(value.get('boot')) or value.get('phase') not in ('reserved','registered','terminal')
            or value.get('kind') not in ('scan','preview','capture','calibration','settings','diagnostic')
            or any(type(value.get(k)) is not int or value[k]<=1 for k in ('owner_pid','owner_start'))):
            raise RegistryError('设备所有权记录无效，需要维护恢复')
        if value['phase']=='registered' and (any(type(value.get(k)) is not int or value[k]<=1 for k in ('wrapper_pid','wrapper_start','pgid','sid'))
            or value['wrapper_pid']!=value['pgid'] or value['wrapper_pid']!=value['sid']):
            raise RegistryError('设备进程登记不完整，需要维护恢复')
        return value
    def _write_path(self,path,value):
        fd,tmp=tempfile.mkstemp(prefix='.camera-owner-',dir=str(path.parent))
        try:
            payload=dict(value,integrity_sha256=checksum(value))
            with os.fdopen(fd,'w') as f:json.dump(payload,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,path)
            d=os.open(str(path.parent),os.O_RDONLY)
            try:os.fsync(d)
            finally:os.close(d)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    def _write(self,value):self._write_path(self.record,value)
    def _read_checked(self,path):
        try:value=json.loads(path.read_text())
        except (OSError,ValueError,TypeError):raise RegistryError('设备见证或审计记录不可读')
        if not isinstance(value,dict):raise RegistryError('设备见证或审计记录无效')
        digest=value.pop('integrity_sha256',None)
        try:ok=isinstance(digest,str) and hmac.compare_digest(digest,checksum(value))
        except (ValueError,TypeError):ok=False
        if not ok:raise RegistryError('设备见证或审计校验失败')
        return value
    def _witness(self):
        value=self._read_checked(self.witness)
        if (value.get('schema')!=1 or value.get('root')!=str(self.root.resolve())
            or not valid_boot(value.get('boot')) or not isinstance(value.get('run'),str)
            or not isinstance(value.get('nonce'),str) or not isinstance(value.get('reserved_sha256'),str)
            or value.get('kind') not in ('scan','preview','capture','calibration','settings','diagnostic')
            or any(type(value.get(k)) is not int or value[k]<=1 for k in ('owner_pid','owner_start'))):
            raise RegistryError('设备见证不完整')
        initial={k:value[k] for k in ('schema','run','nonce','boot','kind','owner_pid','owner_start')}
        initial['phase']='reserved'
        if checksum(initial)!=value['reserved_sha256']:raise RegistryError('预留见证摘要不一致')
        return value
    def _bound_witness(self,value):
        witness=self._witness()
        if (value.get('run')!=witness['run'] or value.get('nonce')!=witness['nonce']
            or value.get('boot')!=witness['boot'] or value.get('owner_pid')!=witness['owner_pid']
            or value.get('owner_start')!=witness['owner_start']
            or value.get('witness_sha256')!=checksum(witness)):
            raise RegistryError('设备见证与所有权记录不一致')
        initial={k:value[k] for k in ('schema','run','nonce','boot','kind','owner_pid','owner_start')}
        initial['phase']='reserved'
        if checksum(initial)!=witness['reserved_sha256']:raise RegistryError('初始预留见证不一致')
        return witness
    def _remove_active(self):
        # Terminal record is durable before either deletion. A retry may find
        # its witness already removed; keep terminal until final deletion.
        try:self.witness.unlink()
        except FileNotFoundError:pass
        self.record.unlink()
        d=os.open(str(self.root),os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    def _terminal(self,value,reason,receipt_sha256=None):
        witness=self._bound_witness(value)
        value=dict(value,phase='terminal',terminal_reason=reason,terminal_witness=witness)
        if receipt_sha256:value['receipt_sha256']=receipt_sha256
        self._write(value)
        self._remove_active()
    def _legacy_profile_markers(self):
        if self.root.resolve()!=canonical_runtime():return []
        base=canonical_runtime().parent
        return [path for mode in ('emg','eeg') for name in ('camera_owner.json','scan_owner.json')
                for path in [base.parent/'profiles'/mode/'orangepi'/'runtime'/name]
                if path.exists() or path.is_symlink()]
    def _reconcile_locked(self):
        if self._legacy_profile_markers():raise RegistryError('其他采集版本存在旧设备所有权记录，需要维护核对')
        value=self._read()
        if value is None:
            if self.witness.exists() or self.witness.is_symlink():raise RegistryError('存在未完成预留见证，需要维护恢复')
            return None
        if value.get('phase')=='terminal':raise RegistryError('设备终结事务未完成，需要维护恢复')
        self._bound_witness(value)
        boot=boot_id()
        if not boot or not isinstance(value.get('boot'),str):raise RegistryError('无法核对本机启动身份')
        # A different boot ID alone is never sufficient proof of cleanup.
        if value.get('phase')!='registered':raise RegistryError('设备所有权登记不完整，需要维护恢复')
        pid,pgid,sid,start=(value.get(k) for k in ('wrapper_pid','pgid','sid','wrapper_start'))
        if any(type(x) is not int or x<=1 for x in (pid,pgid,sid,start)) or pid!=pgid or pid!=sid:
            raise RegistryError('设备所有权身份不完整，需要维护恢复')
        if type(value.get('owner_pid')) is not int or value['owner_pid']<=1 or type(value.get('owner_start')) is not int or value['owner_start']<=0:
            raise RegistryError('控制器身份不完整，需要维护恢复')
        owner=proc_identity(value['owner_pid'])
        if owner and owner['state'] not in ('Z','X'):
            raise RegistryError('原控制器PID仍存在或已被复用，无法确认设备空闲')
        wrapper=proc_identity(pid)
        if wrapper and wrapper['state'] not in ('Z','X'):
            raise RegistryError('设备进程PID仍存在或已被复用，等待维护恢复')
        if group_exists(pgid):raise RegistryError('设备进程组仍存在，等待维护恢复')
        raise RegistryError('设备进程组已消失，仍需显式维护核对')
    def _reserve_locked(self,kind):
        boot=boot_id();identity=proc_identity(os.getpid())
        if not boot or not identity:raise RegistryError('无法确认控制器身份')
        value=dict(schema=1,run=secrets.token_hex(16),nonce=secrets.token_hex(16),boot=boot,kind=kind,phase='reserved',owner_pid=os.getpid(),owner_start=identity['start'])
        witness=dict(schema=1,run=value['run'],nonce=value['nonce'],boot=boot,root=str(self.root.resolve()),kind=kind,owner_pid=value['owner_pid'],owner_start=value['owner_start'],reserved_sha256=checksum(value))
        self._write_path(self.witness,witness)
        value['witness_sha256']=checksum(witness)
        self._write(value);return value['run']
    def reserve(self,kind):
        with self.locked():
            self._reconcile_locked();return self._reserve_locked(kind)
    def register(self,run):
        with self.locked():
            value=self._read();pid=os.getpid();identity=proc_identity(pid)
            if not value or value['run']!=run or value.get('phase')!='reserved' or not identity:
                raise RegistryError('设备启动登记不匹配')
            self._bound_witness(value)
            owner=proc_identity(value.get('owner_pid',-1))
            if not owner or owner['start']!=value.get('owner_start') or owner['state'] in ('Z','X'):
                raise RegistryError('原控制器已退出')
            if value.get('boot')!=boot_id() or identity['pgrp']!=pid or identity['sid']!=pid:
                raise RegistryError('设备启动组身份无效')
            value.update(phase='registered',wrapper_pid=pid,wrapper_start=identity['start'],pgid=pid,sid=pid)
            self._write(value)
    def verify_member(self,run):
        with self.locked():
            value=self._read()
            if not value or value.get('run')!=run or value.get('phase')!='registered':
                raise RegistryError('设备进程没有有效登记')
            self._bound_witness(value)
            pid=os.getpid();identity=proc_identity(pid);leader=proc_identity(value['wrapper_pid'])
            if (not identity or identity['pgrp']!=value['pgid'] or identity['sid']!=value['sid']
                or not leader or leader['start']!=value['wrapper_start'] or leader['state'] in ('Z','X')):
                raise RegistryError('设备进程不属于登记进程组')
            return value

    def cancel_unspawned(self,run):
        # Only the same live controller may cancel before it has attempted spawn.
        with self.locked():
            value=self._read();identity=proc_identity(os.getpid())
            if (not value or value.get('run')!=run or value.get('phase')!='reserved'
                or value.get('owner_pid')!=os.getpid() or not identity or value.get('owner_start')!=identity['start']):
                raise RegistryError('无法确认未启动预留的所有权')
            self._bound_witness(value)
            self._terminal(value,'unspawned_cancelled')
    def verify_reserved(self,run,kind):
        with self.locked():
            value=self._read();identity=proc_identity(os.getpid())
            if (not value or value.get('run')!=run or value.get('kind')!=kind or value.get('phase')!='reserved'
                or value.get('owner_pid')!=os.getpid() or not identity or value.get('owner_start')!=identity['start']):
                raise RegistryError('设备启动预留无效')
            self._bound_witness(value)

    def verify_owner(self,run,kind):
        with self.locked():
            value=self._read();identity=proc_identity(os.getpid())
            if (not value or value.get('run')!=run or value.get('kind')!=kind or value.get('phase')!='registered'
                or value.get('owner_pid')!=os.getpid() or not identity or value.get('owner_start')!=identity['start']):
                raise RegistryError('当前控制器没有有效设备所有权')
            self._bound_witness(value)
            child=proc_identity(value['wrapper_pid'])
            if (not child or child['start']!=value['wrapper_start'] or child['state'] in ('Z','X')
                or child['pgrp']!=value['pgid'] or child['sid']!=value['sid']):
                raise RegistryError('设备进程身份已变化')
            return value
    def finish(self,run):
        with self.locked():
            value=self._read()
            if not value or value.get('run')!=run:raise RegistryError('设备所有权已变化')
            identity=proc_identity(os.getpid())
            if (not identity or value.get('owner_pid')!=os.getpid() or value.get('owner_start')!=identity['start']
                or value.get('boot')!=boot_id()):raise RegistryError('当前控制器不是该设备操作的所有者')
            terminal=value.get('phase')=='terminal' and value.get('terminal_reason')=='normal_confirmed'
            if value.get('phase')!='registered' and not terminal:raise RegistryError('设备所有权不完整，需要维护恢复')
            if terminal and not self.witness.exists():
                embedded=value.get('terminal_witness')
                if not isinstance(embedded,dict) or checksum(embedded)!=value.get('witness_sha256'):
                    raise RegistryError('终结见证不完整，需要维护恢复')
            else:self._bound_witness(value)
            pid=value.get('wrapper_pid');ident=proc_identity(pid)
            if ident and ident['state'] not in ('Z','X'):
                raise RegistryError('设备进程PID仍存在或已被复用')
            if group_exists(value['pgid']):raise RegistryError('设备进程组尚未退出')
            if terminal:self._remove_active()
            else:self._terminal(value,'normal_confirmed')
    def _check_reboot_identities(self,witness,record):
        checks=[(witness.get('owner_pid'),witness.get('owner_start'))]
        if isinstance(record,dict):
            checks.extend([(record.get('owner_pid'),record.get('owner_start')),
                           (record.get('wrapper_pid'),record.get('wrapper_start'))])
        for pid,start in checks:
            if pid is None and start is None:continue
            if type(pid) is not int or pid<=1 or type(start) is not int or start<=1:
                raise RegistryError('历史进程身份不完整，无法维护恢复')
            identity=proc_identity(pid)
            if identity and identity['start']==start and identity['state'] not in ('Z','X'):
                raise RegistryError('历史记录匹配的进程仍在运行')
        if isinstance(record,dict) and record.get('pgid') is not None:
            pgid=record['pgid']
            if type(pgid) is not int or pgid<=1:raise RegistryError('历史进程组无效')
            if group_exists(pgid):raise RegistryError('历史进程组仍存在或被复用')

    def _archive_bytes(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            if path.read_bytes()!=data:raise RegistryError('已有审计原文与当前记录不一致')
            return
        fd,tmp=tempfile.mkstemp(prefix='.audit-',dir=str(path.parent))
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,path)
            for directory in (path.parent,self.root):
                d=os.open(str(directory),os.O_RDONLY)
                try:os.fsync(d)
                finally:os.close(d)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

    def _after_reboot_locked(self,expected_run):
        if not isinstance(expected_run,str) or not re.fullmatch('[0-9a-f]{32}',expected_run):raise RegistryError('必须指定有效的设备操作run')
        if self._legacy_profile_markers():raise RegistryError('其他采集版本存在旧设备所有权记录，需要维护核对')
        current=boot_id()
        if not current:raise RegistryError('无法读取当前内核启动身份')
        try:raw=self.record.read_bytes()
        except FileNotFoundError:raw=None
        except OSError as error:raise RegistryError('无法读取原始设备记录') from error
        if raw is None and not self.witness.exists():
            receipt_path=self.root/'camera_owner_audit'/(expected_run+'.json')
            receipt=self._read_checked(receipt_path)
            witness=receipt.get('witness');witness_hash=checksum(witness) if isinstance(witness,dict) else None
            if (receipt.get('schema')!=1 or receipt.get('run')!=expected_run or receipt.get('new_boot')!=current
                or not witness_hash or receipt.get('witness_sha256')!=witness_hash
                or witness.get('run')!=expected_run or witness.get('root')!=str(self.root.resolve())):
                raise RegistryError('审计收据与当前维护不一致')
            if receipt.get('raw_sha256') is not None:
                try:archive=(self.root/'camera_owner_audit'/(expected_run+'.registry.raw')).read_bytes()
                except OSError:raise RegistryError('审计原文缺失')
                if hashlib.sha256(archive).hexdigest()!=receipt['raw_sha256']:raise RegistryError('审计原文校验失败')
            return dict(run=expected_run,receipt=str(receipt_path),already_terminal=True)
        try:record=json.loads(raw) if raw is not None else None
        except (ValueError,TypeError):raise RegistryError('原记录无法绑定见证，拒绝维护恢复')
        if record is not None and not isinstance(record,dict):raise RegistryError('原记录无法绑定见证')
        # On an interrupted terminal cleanup, the independently durable receipt
        # carries the witnessed evidence even if the active witness was removed.
        if self.witness.exists():
            witness=self._witness();run=witness['run'];witness_hash=checksum(witness)
        elif record and record.get('phase')=='terminal' and isinstance(record.get('run'),str):
            run=record['run']
            try:terminal=self._read()
            except RegistryError:raise RegistryError('终结记录无效，无法恢复')
            if terminal.get('terminal_reason') in ('normal_confirmed','unspawned_cancelled'):
                witness=terminal.get('terminal_witness')
                witness_hash=checksum(witness) if isinstance(witness,dict) else None
                if not witness_hash or terminal.get('witness_sha256')!=witness_hash:raise RegistryError('终结记录缺少有效见证')
            else:
                receipt=self._read_checked(self.root/'camera_owner_audit'/(run+'.json'))
                witness=receipt.get('witness');witness_hash=checksum(witness) if isinstance(witness,dict) else None
                if not witness_hash or receipt.get('witness_sha256')!=witness_hash:raise RegistryError('终结收据缺少有效见证')
        else:raise RegistryError('缺少独立设备见证，不能维护恢复')
        if run!=expected_run:raise RegistryError('设备操作run与维护请求不匹配')
        if witness.get('root')!=str(self.root.resolve()) or not valid_boot(witness.get('boot')):
            raise RegistryError('设备见证的来源无效')
        if witness['boot']==current:raise RegistryError('没有独立证据证明设备已重启')
        if record is not None and (record.get('run')!=run or record.get('nonce')!=witness.get('nonce')):
            raise RegistryError('设备记录与见证run或nonce不匹配')
        if record is not None:
            payload=dict(record);digest=payload.pop('integrity_sha256',None)
            try:record_valid=isinstance(digest,str) and hmac.compare_digest(digest,checksum(payload))
            except (TypeError,ValueError):record_valid=False
            if record_valid and record.get('witness_sha256')!=witness_hash:
                raise RegistryError('设备记录与见证摘要不匹配')
        self._check_reboot_identities(witness,record)
        audit=self.root/'camera_owner_audit';audit.mkdir(parents=True,exist_ok=True)
        raw_hash=hashlib.sha256(raw).hexdigest() if raw is not None else None
        receipt_path=audit/(run+'.json');raw_path=audit/(run+'.registry.raw')
        if receipt_path.exists():
            receipt=self._read_checked(receipt_path)
            if (receipt.get('run')!=run or receipt.get('new_boot')!=current or receipt.get('witness_sha256')!=witness_hash):
                raise RegistryError('已有审计收据与当前维护不一致')
            # After terminal replacement the original raw bytes necessarily
            # differ. Their digest must still match the immutable archive.
            if receipt.get('raw_sha256') is not None:
                try:archive=raw_path.read_bytes()
                except OSError:raise RegistryError('审计原文缺失')
                if hashlib.sha256(archive).hexdigest()!=receipt['raw_sha256']:raise RegistryError('审计原文校验失败')
            if record is not None and record.get('phase')!='terminal' and raw_hash!=receipt.get('raw_sha256'):
                raise RegistryError('维护后设备记录发生变化')
        else:
            if raw is not None:self._archive_bytes(raw_path,raw)
            receipt=dict(schema=1,run=run,old_boot=witness['boot'],new_boot=current,witness=witness,
                witness_sha256=witness_hash,raw_sha256=raw_hash,checks='launcher lock held; matching identities and recorded group absent',at_unix=time.time())
            self._write_path(receipt_path,receipt)
        # Preserve all parseable identity evidence until terminal cleanup.
        initial=dict(schema=1,run=run,nonce=witness['nonce'],boot=witness['boot'],kind=witness['kind'],
            owner_pid=witness['owner_pid'],owner_start=witness['owner_start'],witness_sha256=witness_hash)
        if isinstance(record,dict):
            for key in ('wrapper_pid','wrapper_start','pgid','sid'):
                if key in record:initial[key]=record[key]
        initial.update(phase='terminal',terminal_reason='after_reboot',receipt_sha256=checksum(receipt))
        self._write(initial)
        self._remove_active()
        return dict(run=run,receipt=str(receipt_path))

    def reconcile_after_reboot(self,expected_run):
        # Never wait for a live controller and never signal a historical PID.
        self.root.mkdir(parents=True,exist_ok=True)
        with (self.root/'launcher.lock').open('a+') as maintenance:
            try:fcntl.flock(maintenance,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as error:raise RegistryError('采集服务仍在运行，不能维护恢复') from error
            try:
                opened=os.fstat(maintenance.fileno());current=(self.root/'launcher.lock').stat()
                if (opened.st_dev,opened.st_ino)!=(current.st_dev,current.st_ino):
                    raise RegistryError('维护锁文件已变化')
                with self.locked():return self._after_reboot_locked(expected_run)
            finally:fcntl.flock(maintenance,fcntl.LOCK_UN)

    def reconcile(self):
        with self.locked():return self._reconcile_locked()
    def inspect(self):
        with self.locked():return self._read()
    def inspect_evidence(self):
        # The witness can be the only durable artifact after a crash.
        with self.locked():
            result={'record':None,'witness':None,'errors':[]}
            try:result['record']=self._read()
            except RegistryError as error:result['errors'].append(str(error))
            if self.witness.exists() or self.witness.is_symlink():
                try:result['witness']=self._witness()
                except RegistryError as error:result['errors'].append(str(error))
            return result


def canonical_runtime():
    return (Path(__file__).resolve().parents[3]/'runtime').resolve()


def require_member():
    root=os.environ.get('ORANGEPI_CAMERA_OWNER_ROOT','')
    run=os.environ.get('ORANGEPI_CAMERA_OWNER_RUN','')
    if not root or Path(root).resolve()!=canonical_runtime() or not run:
        raise RegistryError('缺少本机登记设备所有权；请使用采集控制器或维护入口')
    return Registry(canonical_runtime()).verify_member(run)


def validate_owned_command(kind,command):
    # Only controller-selected, audited entry points. This is a trusted local
    # execution chain, not a sandbox against a hostile root/user process.
    from project_paths import BASE,CODE_BASE,SRC
    if not isinstance(command,(list,tuple)) or not command or any(not isinstance(x,str) for x in command):
        raise RegistryError('设备启动命令无效')
    hardware=str(BASE/'.venvs/hardware/bin/python');framework=str(BASE/'.venvs/framework/bin/python')
    routes={
        'scan':[hardware,str(SRC/'acquisition/scan_devices.py')],
        'preview':[hardware,'-u',str(SRC/'acquisition/preview_worker.py')],
        'calibration':[hardware,str(SRC/'acquisition/calibration_probe.py')],
        'settings':[hardware,str(SRC/'control/wrist_settings_worker.py')],
        'diagnostic':[hardware,str(CODE_BASE/'scripts/diagnostics/check_busy_device.py'),'--registered-child'],
    }
    def same_path(a,b):return os.path.realpath(a)==os.path.realpath(b)
    def expected_python(a,b,version):
        return (os.path.normpath(a)==os.path.normpath(b) and os.path.isabs(a)
                and os.path.basename(os.path.realpath(a))=='python'+version)
    if kind=='capture':
        template=[framework,'-u',str(SRC/'framework/framework_recorder.py'),'--output']
        ok=(len(command)==6 and expected_python(command[0],template[0],'3.11') and command[1]==template[1]
            and same_path(command[2],template[2]) and command[3]==template[3]
            and Path(command[4]).is_absolute() and command[5]=='--wait-for-save')
    else:
        template=routes.get(kind);ok=bool(template and len(command)==len(template))
        if ok:
            if not expected_python(command[0],template[0],'3.8'):ok=False
            for a,b in zip(command[1:],template[1:]):
                if (a!=b if b.startswith('-') else not same_path(a,b)):ok=False;break
    if not ok:raise RegistryError('设备启动入口或参数不在受控列表')


def spawn_owned(registry,kind,command,**kwargs):
    """Reserve durably before spawning a private session wrapper."""
    validate_owned_command(kind,command)
    run=registry.reserve(kind)
    return spawn_reserved(registry,run,kind,command,**kwargs),run


def spawn_reserved(registry,run,kind,command,**kwargs):
    validate_owned_command(kind,command)
    registry.verify_reserved(run,kind)
    wrapper=WRAPPER_PATH
    # Incomplete reservations are deliberately retained after any failure.
    process=subprocess.Popen([command[0],str(wrapper),str(registry.root),run,*command],
                             start_new_session=True,**kwargs)
    return process

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Inspect or safely reconcile camera process ownership; no force-clear.')
    parser.add_argument('operation',choices=('inspect','reconcile'))
    parser.add_argument('--root',required=True,help='Shared code-base runtime directory')
    parser.add_argument('--after-reboot',action='store_true',help='Evidence-backed maintenance reconciliation')
    parser.add_argument('--run',help='Exact 32-character run ID shown by inspect')
    options=parser.parse_args();registry=Registry(options.root)
    try:
        if Path(options.root).resolve()!=canonical_runtime():raise RegistryError('必须使用本机权威设备状态目录')
        if options.operation=='inspect':result=registry.inspect_evidence()
        elif options.after_reboot:result=registry.reconcile_after_reboot(options.run)
        else:result=registry.reconcile()
        print(json.dumps({'ok':True,'owner':result},ensure_ascii=False))
    except (RegistryError,OSError) as error:
        print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=False));raise SystemExit(1)
