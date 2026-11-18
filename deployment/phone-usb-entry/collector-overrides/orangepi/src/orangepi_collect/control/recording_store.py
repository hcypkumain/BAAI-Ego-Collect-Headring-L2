"""Local retained takes, reversible deletion and reference release.

No recorder access. Mutations run through the controller's existing action
queue, only after it has verified no preparation/recording worker is alive.
"""
import datetime
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
import yaml

IDENTIFIER=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
CST=datetime.timezone(datetime.timedelta(hours=8))
QUALIFIABLE_STREAMS=frozenset(
    {'cam_ros_'+name for name in ('left','right','bleft','bright')} |
    {'cam_left_wrist','cam_right_wrist','emg_left','emg_right','board_imu','eeg'})


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    raw=(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    try:
        with temporary.open('xb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
        os.replace(str(temporary),str(path));sync_directory(path.parent)
    finally:
        if temporary.exists():temporary.unlink()


def sync_directory(path):
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


def bounded(path,limit=1024*1024):
    path=Path(path);fd=None
    try:
        fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise ValueError('采集元数据不是有界普通文件')
        raw=os.read(fd,limit+1)
        if len(raw)>limit:raise ValueError('采集元数据过大')
        return raw
    finally:
        if fd is not None:os.close(fd)


def capture_evidence_token(path,meta_raw,summary_raw):
    """Bind an upload selection to the frozen QC/START/STOP and stream state.

    Large video/array contents are not hashed on every phone-list refresh, but
    their path, inode, mode, size and mtime are.  The uploader still computes a
    content-hashed manifest before any transfer; this token prevents normal
    queue-to-launch changes from silently replacing the qualification proof.
    """
    path=Path(path);h=hashlib.sha256();h.update(meta_raw);h.update(summary_raw)
    names=('device_selection.json','native_capture_summary.json','start_gate.json',
           'stop_gate.json','qc_report.json','qc_raw_report.json','qc_policy.json',
           'emg_qc_report.json','emg_qc_binding.json','eeg_qc_report.json',
           'eeg_qc_binding.json')
    for name in names:
        candidate=path/name
        try:
            info=candidate.lstat()
            stamp=(name,info.st_mode,info.st_size,info.st_mtime_ns,info.st_ino)
            if stat.S_ISREG(info.st_mode) and info.st_size<=8*1024*1024:
                stamp+=(hashlib.sha256(bounded(candidate,8*1024*1024)).hexdigest(),)
        except OSError:
            stamp=(name,None)
        h.update(repr(stamp).encode())
    try:selection=json.loads(bounded(path/'device_selection.json',2*1024*1024))
    except (OSError,ValueError,TypeError):selection={}
    required=selection.get('required_device_contract',{})
    selected=selection.get('selected',[])
    if not isinstance(required,dict):required={}
    if not isinstance(selected,list):selected=[]
    required_names=required.get('required_devices',[])
    if not isinstance(required_names,list):required_names=[]
    streams=(set(x for x in selected if isinstance(x,str) and x in QUALIFIABLE_STREAMS)|
             set(x for x in required_names if isinstance(x,str) and x in QUALIFIABLE_STREAMS))
    for stream in sorted(streams):
        folder=path/stream
        try:
            for current,dirs,files in os.walk(folder,followlinks=False):
                current=Path(current)
                for name in sorted(dirs+files):
                    item=current/name
                    try:
                        info=item.lstat()
                        stamp=(item.relative_to(path).as_posix(),info.st_mode,info.st_size,
                               info.st_mtime_ns,info.st_ino)
                    except OSError:stamp=(item.relative_to(path).as_posix(),None)
                    h.update(repr(stamp).encode())
        except OSError:
            h.update((stream+':unreadable').encode())
    return h.hexdigest()


class RecordingStore:
    def __init__(self,base,project,ledger,session_provider=None,volume_provider=None,clock=time.time):
        self.base=Path(base);self.project=Path(project).resolve();self.ledger=ledger;self.clock=clock
        self.session_provider=session_provider;self.volume_provider=volume_provider
        self.operations=self.base/'runtime/recording_operations'

    def roots(self):
        if self.session_provider is None:
            import storage_paths
            volume=storage_paths.volume_root();session=volume/'data'/storage_paths.config()['shift']
        else:session=self.session_provider();volume=self.volume_provider()
        session=Path(session).resolve();volume=Path(volume).resolve()
        session.relative_to(volume)
        if session==volume:raise ValueError('采集目录配置无效')
        return session,volume

    def path(self,identifier):
        if not isinstance(identifier,str) or not IDENTIFIER.fullmatch(identifier) or identifier in ('.','..'):raise ValueError('采集条目标识无效')
        root,_=self.roots();path=root/identifier
        if path.is_symlink() or not path.is_dir() or path.resolve().parent!=root:raise ValueError('采集条目不存在或路径已变化')
        return path

    def metadata(self,path,ledger_cache=None):
        path=Path(path);source=path/'meta.yaml'
        if source.exists() or source.is_symlink():raw=bounded(source);meta=yaml.safe_load(raw) or {}
        else:source=path/'meta.json';raw=bounded(source);meta=json.loads(raw)
        if not isinstance(meta,dict):raise ValueError('采集元数据无效')
        pi=meta.get('pi') if isinstance(meta.get('pi'),dict) else meta
        selection=meta.get('reference_selection') or pi.get('reference_selection') or {}
        if not isinstance(selection,dict):selection={}
        if not isinstance(selection.get('asset',{}),dict):raise ValueError('参考图凭据无效')
        asset=selection.get('asset') or {};reference=asset.get('relative_path') or meta.get('reference_image') or pi.get('reference_image') or meta.get('environment')
        image=None
        if isinstance(reference,str) and reference:
            candidate=Path(reference)
            if candidate.is_absolute():image=candidate.resolve()
            else:image=self.project/(reference if reference.startswith('configs/environments/') else 'configs/environments/'+reference)
            image=image.resolve();image.relative_to((self.project/'configs/environments').resolve())
            if not image.is_file():image=None
        summary={};summary_raw=b''
        if (path/'capture_summary.json').is_file():
            summary_raw=bounded(path/'capture_summary.json');summary=json.loads(summary_raw)
            if not isinstance(summary,dict):raise ValueError('采集总结格式无效')
        qc_report={}
        try:
            raw_qc=bounded(path/'qc_report.json',8*1024*1024);qc_report=json.loads(raw_qc)
            if not isinstance(qc_report,dict):qc_report={}
        except (OSError,ValueError,TypeError):pass
        signature=path.stat();token=hashlib.sha256(str((signature.st_dev,signature.st_ino)).encode()+
            capture_evidence_token(path,raw,summary_raw).encode()).hexdigest()
        used=None
        if image:
            ledger_path=image.parent/'used.yaml'
            if ledger_cache is None:book=self.ledger.read(ledger_path)
            else:
                try:info=ledger_path.stat();signature=(info.st_mtime_ns,info.st_size)
                except FileNotFoundError:signature=None
                cached=ledger_cache.get(str(ledger_path))
                if cached is None or cached[0]!=signature:
                    cached=(signature,self.ledger.read(ledger_path));ledger_cache[str(ledger_path)]=cached
                book=cached[1]
            used=book.get('used',{}).get(image.name)
        collected=used is not None and self.ledger.valid_used(used,image)
        owns=bool(isinstance(used,dict) and str(used.get('session',''))==str(path))
        status=meta.get('status') or pi.get('status') or summary.get('status') or 'unconfirmed'
        if (path/'recording_deleted.json').exists():status='deleted'
        qualified,qualification_error=self._qualification(path,status,summary,pi)
        successful=qualified and collected and (owns or meta.get('retention_state')=='kept')
        failed_kept=status not in ('success','deleted','archived','cancelled') and meta.get('retention_state')=='kept_failed'
        pending=status=='pending_qc' and summary.get('qc_state')=='pending' and meta.get('retention_state')=='kept_pending_qc'
        # Retention and quality are separate facts. A retained but incomplete
        # or ERROR take remains visible for review, while never becoming a
        # successful reference attempt or eligible normal upload.
        retained_state=meta.get('retention_state') in ('kept','kept_failed','kept_pending_qc')
        kept=retained_state or successful or failed_kept or pending
        stamp=summary.get('start_unix_ns',0)/1e9 or meta.get('start_time') or pi.get('session_created_unix') or meta.get('session_created_unix') or source.stat().st_mtime
        if not isinstance(stamp,(int,float)) or not math.isfinite(stamp):stamp=source.stat().st_mtime
        retained_at=meta.get('retained_at_unix') or summary.get('stop_unix_ns',0)/1e9 or stamp
        if not isinstance(retained_at,(int,float)) or not math.isfinite(retained_at):retained_at=stamp
        day=datetime.datetime.fromtimestamp(retained_at,CST).date().isoformat()
        duration=meta.get('duration') or 0
        if not duration and summary.get('start_unix_ns'):duration=max(0,summary.get('stop_unix_ns',0)-summary['start_unix_ns'])/1e9
        return dict(id=path.name,token=token,created_unix=stamp,retained_at_unix=retained_at,day=day,duration=duration,
            reference_name=asset.get('name') or (image.stem if image else '参考图信息不可用'),
            reference_path=image.relative_to(self.project).as_posix() if image else None,
            reference_sha256=asset.get('sha256') or meta.get('reference_sha256') or pi.get('reference_sha256'),
            status=status,retained=kept,reference_state='pending_qc' if pending else 'collected' if successful else 'uncollected',
            qc_state='pending' if pending else meta.get('qc_state') or ('completed' if qualified else 'incomplete'),
            retention_kind='pending_qc' if pending else 'successful' if successful else 'unsuccessful' if kept else 'not_retained',
            qualified=successful,qualification_error=qualification_error,
            reference_attempt_succeeded=successful,
            upload_state='pending_qc' if pending else 'qualified_for_upload' if successful else 'diagnostic_only' if kept else 'excluded',qc_level=qc_report.get('level') or meta.get('qc_level') or summary.get('qc_level') or ('ERROR' if status in ('error','qc_error') else None),
            selected_devices=meta.get('selected_devices') or pi.get('selected_devices') or [],
            _image=image)

    def _qualification(self,path,status,summary,meta):
        """Return full-QC qualification using frozen contract and saved data."""
        from required_device_contract import resolve_selection,data_warnings,qualification_errors
        from capture_qc_receipt import verify as verify_qc_receipt
        if status!='success' or summary.get('status')!='success' or summary.get('errors'):
            return False,'采集未以无错误状态成功收尾'
        try:
            selection=json.loads(bounded(Path(path)/'device_selection.json',2*1024*1024))
            if not isinstance(selection,dict):raise ValueError('冻结设备选择无效')
            config=selection.get('device_config')
            contract,errors,legacy=resolve_selection(config,selection)
            if errors:return False,'冻结设备合同无效：'+'；'.join(errors)
            report=json.loads(bounded(Path(path)/'qc_report.json',8*1024*1024))
            if not isinstance(report,dict):raise ValueError('完整质检报告无效')
        except (OSError,ValueError,TypeError,KeyError,RecursionError) as error:
            return False,'完整质检证明缺失或无效：'+str(error)
        level=report.get('level')
        if report.get('scope','full')!='full':return False,'本段只有局部质检，完整质检仍待完成'
        if level not in ('INFO','WARN'):return False,'完整质检未通过：'+str(level or '缺少等级')
        if (summary.get('qc_state')=='pending' or
                summary.get('qc_mode') in ('deferred','emg_only','eeg_only') and summary.get('qc_state')!='completed'):
            return False,'本段质检模式尚未完成完整质检'
        if summary.get('qc_level') not in (None,level):return False,'质检报告与采集摘要等级不一致'
        streams=report.get('streams')
        if not isinstance(streams,dict) or not set(contract.get('required_devices',())).issubset(streams):
            return False,'完整质检报告未覆盖冻结合同中的全部必需设备'
        if any(not isinstance(streams[name],dict) or streams[name].get('level') not in ('INFO','WARN')
               for name in contract.get('required_devices',())):
            return False,'必需设备的单流质检缺失或未通过'
        errors=qualification_errors(path,summary,contract,report,legacy=legacy)
        if errors:return False,'必需数据不完整：'+'；'.join(errors[:12])
        receipt_errors=verify_qc_receipt(path)
        if receipt_errors:
            return False,'缺少可验证的完整质检回执；请重新执行本机完整质检：'+'；'.join(receipt_errors[:8])
        warnings=data_warnings(summary,legacy)
        return True,('历史记录缺少新版在线证明：'+'；'.join(warnings[:8])) if warnings else ''

    def entries(self,day='today'):
        if day=='today':day=datetime.datetime.fromtimestamp(self.clock(),CST).date().isoformat()
        if day!='all':
            try:datetime.date.fromisoformat(day)
            except (ValueError,TypeError):raise ValueError('采集日期无效')
        root,_=self.roots();items=[]
        if not root.is_dir():return items,day
        for path in root.iterdir():
            if path.is_symlink() or not path.is_dir() or not IDENTIFIER.fullmatch(path.name):continue
            try:value=self.metadata(path)
            except (OSError,ValueError,TypeError):continue
            if value['status'] in ('deleted','archived'):continue
            if day=='all' or value['day']==day:
                value.pop('_image',None);items.append(value)
        items.sort(key=lambda value:(value['created_unix'],value['id']),reverse=True)
        return items,day

    def page(self,day='today',offset=0,limit=20):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:raise ValueError('采集列表页码无效')
        items,day=self.entries(day);kept=sum(item['retained'] for item in items)
        return dict(version=1,day=day,total=len(items),retained_count=kept,items=items[offset:offset+limit],
            next_offset=offset+limit if offset+limit<len(items) else None)

    def upload_manifest(self,day='today'):
        items,day=self.entries(day);root,_=self.roots()
        return dict(version=1,day=day,created_at_unix=self.clock(),scope='retained_local_takes_for_manual_cloud_upload',
            note='只包含完整QC通过且全部必需设备数据齐全的采集；保留的失败数据仅供本地诊断，不进入标准上传队列。',
            recordings=[dict(item,relative_directory='data/'+root.name+'/'+item['id']) for item in items if item['retained'] and item.get('qualified') is True])

    def delete(self,identifier,token):
        from cloud_common import session_lock
        with session_lock(self.base,identifier):return self._delete_unlocked(identifier,token)

    def _delete_unlocked(self,identifier,token):
        path=self.path(identifier);value=self.metadata(path)
        if value['token']!=token:raise ValueError('采集条目已变化，请刷新后再删除')
        if value['reference_path']:
            image=(self.project/value['reference_path']).resolve()
            image.relative_to((self.project/'configs/environments').resolve())
            with (self.base/'runtime/ledger.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                ledger_value=self.ledger.read(image.parent/'used.yaml')
                owner=ledger_value.get('used',{}).get(image.name)
                attempts=ledger_value.get('attempts',{}).get(image.name,[])
                if not isinstance(attempts,list) or any(not isinstance(x,dict) for x in attempts):raise ValueError('参考图历史损坏，未删除，请人工核对')
                other=any(x.get('session')!=str(path) and not x.get('deleted') and x.get('status') in ('success','pending_qc') for x in attempts)
                if isinstance(owner,dict) and owner.get('session')==str(path) and other:raise ValueError('参考图还有其他已保存记录，为防错误释放未删除，请人工核对')
        _,volume=self.roots();target_root=volume/'deleted-recordings';target_root.mkdir(exist_ok=True)
        target=target_root/(identifier+'--'+uuid.uuid4().hex[:12])
        if target_root.is_symlink() or target_root.resolve().parent!=volume or path.stat().st_dev!=target_root.stat().st_dev:raise ValueError('回收站不在同一采集存储卷')
        operation=dict(version=1,id=uuid.uuid4().hex,kind='delete_recording',source=str(path),target=str(target),
            session_id=identifier,reference_path=value['reference_path'],created_at_unix=self.clock(),phase='prepared')
        journal=self.operations/(operation['id']+'.json');atomic_json(journal,operation)
        return self._finish_delete(journal,operation)

    def abandon_preparation(self,path):
        path=self.path(Path(path).name)
        if (path/'start_gate.json').exists():raise ValueError('已开录目录不能按准备放弃')
        return self.discard_current(path,preparation=True)

    def discard_current(self,path,preparation=False):
        """Purge only the stopped, explicitly discarded current take.

        Existing retained takes keep their separate deletion/recycle behavior.
        A journal outside the take makes interrupted cleanup recoverable.
        """
        from cloud_common import session_lock
        requested=Path(path);source=self.path(requested.name)
        if requested.is_symlink() or requested.resolve()!=source.resolve():raise ValueError('放弃路径不是当前采集目录')
        with session_lock(self.base,source.name):
            root,volume=self.roots()
            meta_path=source/'meta.yaml'
            meta=yaml.safe_load(bounded(meta_path)) if meta_path.is_file() else json.loads(bounded(source/'meta.json'))
            if not isinstance(meta,dict):raise ValueError('本段元数据无效')
            if meta.get('retention_state') in ('kept','kept_failed') or meta.get('retained_at_unix'):
                raise ValueError('已保留的数据请在采集管理中处理')
            if preparation:
                if (source/'start_gate.json').exists():raise ValueError('已开录目录不能按准备放弃')
            else:
                request=json.loads(bounded(source/'discard_request.json'))
                if request.get('session_id')!=source.name or request.get('directory')!=str(source.resolve()):
                    raise ValueError('本段放弃请求不匹配')
                stop=json.loads(bounded(source/'stop_gate.json'))
                if stop.get('recording_active') is not False:raise ValueError('本段尚未确认停止')
                # Never purge a failed or unverifiable recording on retry. The
                # recorder can replace status with 'discarded', so inspect both
                # the original native summary and the final summary.
                started=(source/'start_gate.json').exists()
                summaries=[]
                for name in ('native_capture_summary.json','capture_summary.json'):
                    f=source/name
                    if not f.exists():continue
                    try:value=json.loads(bounded(f))
                    except (OSError,ValueError,TypeError):
                        raise ValueError('采集结果无法核验，已保留本段数据，请在采集管理中检查')
                    if not isinstance(value,dict):raise ValueError('采集结果无效，已保留本段数据，请在采集管理中检查')
                    summaries.append(value)
                if started and not (source/'native_capture_summary.json').is_file():
                    raise ValueError('录制结果缺失，已保留本段数据，请在采集管理中检查')
                if any(v.get('status') in ('error','failed','interrupted','qc_error') or v.get('capture_status') in ('error','failed','interrupted','qc_error') or bool(v.get('errors')) for v in summaries):
                    raise ValueError('本段采集出现错误，已保留已有数据，请在采集管理中检查')
            pi=meta.get('pi') if isinstance(meta.get('pi'),dict) else meta
            reference=meta.get('reference_image') or pi.get('reference_image')
            if reference:
                image=(self.project/reference).resolve();image.relative_to((self.project/'configs/environments').resolve())
                reference=image.relative_to(self.project).as_posix()
            destination=volume/'.discarding-recordings';destination.mkdir(exist_ok=True)
            if destination.is_symlink() or destination.resolve().parent!=volume or source.stat().st_dev!=destination.stat().st_dev:
                raise ValueError('放弃清理目录不在同一采集卷')
            files=total=0
            for current,dirs,names in os.walk(str(source),followlinks=False):
                parent=Path(current)
                for name in dirs+names:
                    if (parent/name).is_symlink():raise ValueError('放弃目录包含链接，已停止清理')
                for name in names:files+=1;total+=(parent/name).stat().st_size
            operation=dict(version=1,id=uuid.uuid4().hex,kind='discard_recording',session_id=source.name,
                source=str(source),target=str(destination/(source.name+'--'+uuid.uuid4().hex)),
                source_identity=[source.stat().st_dev,source.stat().st_ino],
                reference_path=reference,created_at_unix=self.clock(),phase='prepared',files=files,bytes=total)
            from daily_collection import take_snapshot
            operation['daily_snapshot']=take_snapshot(self,source)
            journal=self.operations/(operation['id']+'.json');atomic_json(journal,operation)
            return self._finish_discard(journal,operation)

    def _finish_discard(self,journal,operation):
        root,volume=self.roots();source=Path(operation['source']);target=Path(operation['target'])
        if (source.parent!=root or source.name!=operation['session_id'] or
            target.parent!=volume/'.discarding-recordings' or not target.name.startswith(source.name+'--') or
            source.is_symlink() or target.is_symlink() or target.parent.is_symlink() or
            target.parent.resolve()!=volume/'.discarding-recordings'):
            raise ValueError('放弃清理路径不在采集目录内')
        if source.exists():
            if target.exists():raise ValueError('放弃源目录和临时目录同时存在')
            if operation['source_identity']!=[source.stat().st_dev,source.stat().st_ino]:raise ValueError('放弃源目录已被替换')
            atomic_json(source/'discard_cleanup.json',dict(operation_id=operation['id']))
            os.rename(str(source),str(target));sync_directory(root);sync_directory(target.parent)
        if target.exists():
            identity=[target.stat().st_dev,target.stat().st_ino]
            if operation.get('target_identity') is None:
                marker=json.loads(bounded(target/'discard_cleanup.json'))
                if marker.get('operation_id')!=operation['id']:raise ValueError('放弃清理凭据不匹配')
                operation['target_identity']=identity
            elif operation['target_identity']!=identity:raise ValueError('放弃清理目录已变化')
            operation['phase']='purging';atomic_json(journal,operation)
            shutil.rmtree(str(target));sync_directory(target.parent)
        elif operation.get('phase') not in ('purging','complete'):
            raise ValueError('放弃清理目录不存在，请核对日志')
        image=self.project/operation['reference_path'] if operation.get('reference_path') else None
        released=self.ledger.release(image,source) if image else False
        operation.update(phase='complete',reference_released=released,completed_at_unix=self.clock());atomic_json(journal,operation)
        return dict(id=source.name,deleted=True,files=operation['files'],bytes=operation['bytes'],qc_skipped=True)

    def retain_error(self,identifier,token):
        from cloud_common import session_lock
        with session_lock(self.base,identifier):return self._retain_error_unlocked(identifier,token)

    def _retain_error_unlocked(self,identifier,token):
        path=self.path(identifier);value=self.metadata(path)
        if value['token']!=token:raise ValueError('采集条目已变化，请刷新后再保留')
        if value['status'] in ('success','cancelled','unconfirmed','deleted','archived'):raise ValueError('此操作仅用于保留ERROR或未成功的采集数据')
        target=path/'meta.yaml';meta=yaml.safe_load(bounded(target)) if target.is_file() else json.loads(bounded(path/'meta.json'))
        if not isinstance(meta,dict):raise ValueError('采集元数据无效')
        from orangepi_collect.storage.durability import sync_session
        proof=sync_session(path)
        meta.update(storage_durability=proof,retention_state='kept_failed',retained_at_unix=self.clock(),reference_attempt_succeeded=False,
            upload_state='retained_unsuccessful_for_upload',reference_state=value['reference_state'])
        self.ledger.atomic_yaml(target,meta)
        if value['_image'] is not None:self.ledger.retain_error(value['_image'],path)
        return dict(id=identifier,retained=True,reference_attempt_succeeded=False)

    def _finish_delete(self,journal,operation):
        root,volume=self.roots();source=Path(operation['source']);target=Path(operation['target'])
        if source.parent!=root or target.parent!=(volume/'deleted-recordings') or source.name!=operation['session_id']:raise ValueError('删除事务路径不在采集目录内')
        if source.is_symlink() or target.is_symlink() or target.parent.is_symlink():raise ValueError('删除事务不能使用链接目录')
        image=self.project/operation['reference_path'] if operation['reference_path'] else None
        with ((self.base/'runtime/ledger.lock').open('a') if image else contextlib.nullcontext()) as ledger_lock:
            if image:
                fcntl.flock(ledger_lock,fcntl.LOCK_EX)
                image=image.resolve();image.relative_to((self.project/'configs/environments').resolve())
                value=self.ledger.read(image.parent/'used.yaml');owner=value.get('used',{}).get(image.name)
                attempts=value.get('attempts',{}).get(image.name,[])
                if not isinstance(attempts,list) or any(not isinstance(x,dict) for x in attempts):raise ValueError('参考图历史损坏，未删除，请人工核对')
                other=any(x.get('session')!=str(source) and not x.get('deleted') and x.get('status') in ('success','pending_qc') for x in attempts)
                if isinstance(owner,dict) and owner.get('session')==str(source) and other:
                    # Abort the durable transaction as well: never leave a
                    # permanently replaying journal after a conservative refusal.
                    if target.exists() and not source.exists():
                        recorded=json.loads(bounded(target/'recording_deleted.json'))
                        if recorded.get('operation_id')!=operation['id']:raise ValueError('回收站删除凭据不匹配')
                        os.rename(str(target),str(source));sync_directory(root);sync_directory(target.parent)
                    if source.exists() and (source/'recording_deleted.json').exists():
                        recorded=json.loads(bounded(source/'recording_deleted.json'))
                        if recorded.get('operation_id')!=operation['id']:raise ValueError('删除标记凭据不匹配')
                        (source/'recording_deleted.json').unlink();sync_directory(source)
                    operation.update(phase='complete',outcome='refused',reference_restoration={'status':'manual','reason':'参考图还有其他已保存记录'},completed_at_unix=self.clock());atomic_json(journal,operation)
                    raise ValueError('参考图还有其他已保存记录，为防错误释放未删除，请人工核对')
            if source.exists():
                if target.exists():raise ValueError('删除目标已存在，请检查事务日志')
                atomic_json(source/'recording_deleted.json',dict(operation_id=operation['id'],deleted_at_unix=operation['created_at_unix'],reference_path=operation['reference_path']))
                os.rename(str(source),str(target));sync_directory(root);sync_directory(target.parent)
            elif not target.is_dir():raise ValueError('删除事务的源和目标均不存在')
            marker=json.loads(bounded(target/'recording_deleted.json'))
            if marker.get('operation_id')!=operation['id']:raise ValueError('回收站删除凭据不匹配')
            released=self.ledger._release_locked(image,source) if image else False
            if image:
                from task_delete_restore import restore_saved_reference
                restoration=restore_saved_reference(self.base,self.project,self.ledger,operation['reference_path'],source,already_locked=True)
            else:restoration={'status':'manual','reason':'本段缺少参考图关联'}
            released=released or restoration['status']=='restored'
            operation.update(phase='complete',reference_released=released,reference_restoration=restoration,completed_at_unix=self.clock());atomic_json(journal,operation)
            return dict(id=operation['session_id'],deleted=True,reference_released=released,reference_restoration=restoration,
                message='已删除本段采集，参考图与可证明的任务进度已恢复。' if restoration['status']=='restored' else '已删除本段采集，但参考图进度未自动恢复：'+restoration['reason'])

    def recover(self):
        if not self.operations.is_dir():return
        for journal in sorted(self.operations.glob('*.json')):
            value=json.loads(bounded(journal,65536))
            if value.get('kind')=='delete_recording' and value.get('phase')!='complete':
                from cloud_common import session_lock
                with session_lock(self.base,value['session_id']):self._finish_delete(journal,value)
            elif value.get('kind')=='discard_recording' and value.get('phase')!='complete':
                from cloud_common import session_lock
                with session_lock(self.base,value['session_id']):self._finish_discard(journal,value)
