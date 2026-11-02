"""Local retained takes, reversible deletion and reference release.

No recorder access. Mutations run through the controller's existing action
queue, only after it has verified no preparation/recording worker is alive.
"""
import datetime
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

    def metadata(self,path):
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
        signature=path.stat();token=hashlib.sha256(str((signature.st_dev,signature.st_ino)).encode()+raw+summary_raw).hexdigest()
        used=self.ledger.read(image.parent/'used.yaml').get('used',{}).get(image.name) if image else None
        collected=used is not None and self.ledger.valid_used(used)
        owns=bool(isinstance(used,dict) and str(used.get('session',''))==str(path))
        status=meta.get('status') or pi.get('status') or summary.get('status') or 'unconfirmed'
        if (path/'recording_deleted.json').exists():status='deleted'
        successful=status=='success' and summary.get('status','success')=='success' and collected and (owns or meta.get('retention_state')=='kept')
        failed_kept=status not in ('success','deleted','archived','cancelled') and meta.get('retention_state')=='kept_failed'
        kept=successful or failed_kept
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
            status=status,retained=kept,reference_state='collected' if collected else 'uncollected',
            retention_kind='successful' if successful else 'unsuccessful' if failed_kept else 'not_retained',reference_attempt_succeeded=successful,
            upload_state='retained_for_upload' if successful else 'retained_unsuccessful_for_upload' if failed_kept else 'excluded',qc_level=meta.get('qc_level') or summary.get('qc_level') or ('ERROR' if status in ('error','qc_error') else None),
            selected_devices=meta.get('selected_devices') or pi.get('selected_devices') or [],
            _image=image)

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
            note='按确认保留日期分组；成功/WARN标记已采集，用户保留的ERROR或失败数据明确标记未成功，不代表云端上传结果。',
            recordings=[dict(item,relative_directory='data/'+root.name+'/'+item['id']) for item in items if item['retained']])

    def delete(self,identifier,token):
        from cloud_common import session_lock
        with session_lock(self.base,identifier):return self._delete_unlocked(identifier,token)

    def _delete_unlocked(self,identifier,token):
        path=self.path(identifier);value=self.metadata(path)
        if value['token']!=token:raise ValueError('采集条目已变化，请刷新后再删除')
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
        if source.exists():
            if target.exists():raise ValueError('删除目标已存在，请检查事务日志')
            atomic_json(source/'recording_deleted.json',dict(operation_id=operation['id'],deleted_at_unix=operation['created_at_unix'],reference_path=operation['reference_path']))
            os.rename(str(source),str(target));sync_directory(root);sync_directory(target.parent)
        elif not target.is_dir():raise ValueError('删除事务的源和目标均不存在')
        marker=json.loads(bounded(target/'recording_deleted.json'))
        if marker.get('operation_id')!=operation['id']:raise ValueError('回收站删除凭据不匹配')
        image=self.project/operation['reference_path'] if operation['reference_path'] else None
        released=self.ledger.release(image,source) if image else False
        operation.update(phase='complete',reference_released=released,completed_at_unix=self.clock());atomic_json(journal,operation)
        return dict(id=operation['session_id'],deleted=True,reference_released=released,
            message='已删除本段采集，参考图已释放，可重新采集。' if released else '已删除本段采集，参考图状态已重新核对。')

    def recover(self):
        if not self.operations.is_dir():return
        for journal in sorted(self.operations.glob('*.json')):
            value=json.loads(bounded(journal,65536))
            if value.get('kind')=='delete_recording' and value.get('phase')!='complete':self._finish_delete(journal,value)
            elif value.get('kind')=='discard_recording' and value.get('phase')!='complete':
                from cloud_common import session_lock
                with session_lock(self.base,value['session_id']):self._finish_discard(journal,value)
