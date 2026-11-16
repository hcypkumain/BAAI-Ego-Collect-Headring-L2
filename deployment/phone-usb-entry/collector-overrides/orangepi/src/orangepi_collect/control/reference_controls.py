"""Controller-owned reference queue, immutable episode proof and list selection.

No hardware access. Catalog mutations share the exact store passed to Dashboard.
Heartbeat/status methods only read cached values. Hashing occurs at prepare.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time
import uuid

from reference_catalog import ReferenceCatalog, CatalogError, unique_json, valid_id, _durable, _encoded
from reference_credentials import ReferenceCredentials
from session_naming import collector_id, reserve_session_dir, AllocationDurabilityError

SAFE_STATES = ('device_confirm', 'idle', 'failed_wait', 'exhausted')
IDENTITY_FIELDS = ('collector_id', 'session', 'session_id', 'session_timezone',
                   'session_created_unix', 'session_name_format')


def _preferences(base):
    path=Path(base)/'configs/reference_control.json'
    descriptor=None
    try:
        descriptor=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size>4096:
            raise ValueError('参考图设置文件无效')
        raw=os.read(descriptor,4097)
        if len(raw)>4096:raise ValueError('参考图设置文件过大')
        value=unique_json(raw)
    finally:
        if descriptor is not None:os.close(descriptor)
    if (not isinstance(value,dict) or set(value)!={'version','collector_id','selected_list_id'}
            or type(value['version']) is not int or value['version']!=1
            or not valid_id(value['selected_list_id'])):
        raise ValueError('参考图设置格式无效')
    value['collector_id']=collector_id(value['collector_id'])
    return value


def _task_block_policy(base):
    path=Path(base)/'configs/task_block_policy.json'
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>1048576:raise ValueError('无效的任务分组配置')
        raw=os.read(fd,1048577)
    finally:os.close(fd)
    if len(raw)>1048576:raise ValueError('任务分组配置过大')
    value=unique_json(raw)
    if (not isinstance(value,dict) or set(value) not in ({'version','list_ids','block_size'},{'version','list_ids','block_size','count_basis'})
            or type(value['version']) is not int or value['version']!=1
            or not isinstance(value['list_ids'],list) or not value['list_ids']
            or any(not valid_id(i) for i in value['list_ids'])
            or len(set(value['list_ids']))!=len(value['list_ids'])
            or not (value['block_size']=='all' or (type(value['block_size']) is int and 1<=value['block_size']<=5000))
            or value.get('count_basis','saved') not in ('qualified','saved')):
        raise ValueError('任务分组配置无效')
    return value,hashlib.sha256(raw).hexdigest()


def _save_preferences(base,value):
    path=Path(base)/'configs/reference_control.json'
    if path.is_symlink():raise ValueError('参考图设置路径无效')
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
    descriptor,temporary=tempfile.mkstemp(prefix='.reference-control-',dir=str(path.parent))
    committed=False
    try:
        with os.fdopen(descriptor,'wb') as output:
            output.write(raw);output.flush()
            if hasattr(os,'fchmod'):os.fchmod(output.fileno(),0o600)
            else:os.chmod(temporary,0o600)
            os.fsync(output.fileno())
        os.replace(temporary,str(path));committed=True
        if os.name!='nt':
            directory=os.open(str(path.parent),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory)
            finally:os.close(directory)
    except OSError as error:
        raise CatalogError('列表选择未确认保存，请检查磁盘后重新选择','storage_error',503,committed) from error
    finally:
        try:os.unlink(temporary)
        except FileNotFoundError:pass


def _legacy_paths(project,ledger):
    library=Path(project)/'configs/environments'
    return [Path(path) for path in ledger.images()
            if not {'_uploads','_uploaded'}.intersection(Path(path).relative_to(library).parts)]


def _completed_paths(project,ledger):
    """Run only on startup/progress/catalog changes; never from HTTP status."""
    cache={};result=[]
    for image in ledger.images():
        image=Path(image);path=image.parent/'used.yaml'
        if path not in cache:cache[path]=ledger.read(path).get('used',{})
        entry=cache[path].get(image.name)
        if entry is not None and ledger.valid_used(entry,image):
            result.append(image.relative_to(project).as_posix())
    return tuple(sorted(set(result)))


def _reserved_paths(project,ledger):
    """Pending QC reserves a reference for deduplication but is not completion."""
    cache={};result=[]
    for image in ledger.images():
        image=Path(image);path=image.parent/'used.yaml'
        if path not in cache:cache[path]=ledger.read(path).get('used',{})
        entry=cache[path].get(image.name)
        if entry is not None and ledger.reserved_used(entry,image):
            result.append(image.relative_to(project).as_posix())
    return tuple(sorted(set(result)))


def _first_image_index(snapshot,selected,project):
    if not selected:return None
    requested=Path(selected).expanduser()
    candidates={str(path.resolve()) for path in (requested,Path(project)/requested,
                Path(project)/'configs/environments'/requested) if path.is_file()}
    found=[]
    for index,item in enumerate(snapshot['items']):
        path=Path(project)/item['relative_path']
        if str(path.resolve()) in candidates or path.name==str(selected) or path.stem==str(selected):
            found.append(index)
    if len(found)!=1:raise ValueError('指定参考图不存在或名称不唯一，请选择具体图片')
    return found[0]


def reference_cli_queue(base,project,ledger,selected=None):
    """Maintenance --list-images follows the selected scope instead of all disk files."""
    catalog=ReferenceCatalog(base,project=project)
    catalog.import_legacy(_legacy_paths(project,ledger))
    preferences=_preferences(base)
    identifier='all' if selected and not catalog.single_list_mode else preferences['selected_list_id']
    snapshot=catalog.freeze_list(identifier,catalog.revision,include_completed=True)
    completed=set(_completed_paths(project,ledger));reserved=set(_reserved_paths(project,ledger));first=_first_image_index(snapshot,selected,project)
    return [Path(project)/item['relative_path'] for index,item in enumerate(snapshot['items'])
            if (first is None or index>=first) and
               item['relative_path'] not in reserved and
               (index==first or item['relative_path'] not in completed)]


class ReferenceControlsMixin:
    def _assert_controller_thread(self):
        if (getattr(self,'_controller_thread',None) is not threading.current_thread()
                or getattr(self,'_controller_thread_id',None)!=threading.get_ident()):
            raise RuntimeError('controller actor thread required')
        if getattr(self,'_task_block_write_active',False):
            raise RuntimeError('task block write in progress')

    def _reference_mutation_started(self):
        self._assert_controller_thread()
        if type(getattr(self,'ui_revision',None)) is not int or self.ui_revision<0:
            raise RuntimeError('controller revision unavailable')
        self.ui_revision+=1

    def init_references(self,selected_image=None,explicit_image=False):
        if hasattr(self,'_controller_thread') or hasattr(self,'_controller_thread_id'):
            self._assert_controller_thread()
        else:
            self._controller_thread=threading.current_thread();self._controller_thread_id=threading.get_ident()
            self.ui_revision=0;self._task_block_write_active=False
        from project_paths import BASE,PROJECT
        import ledger
        self._reference_base=Path(BASE);self._reference_project=Path(PROJECT);self._reference_ledger=ledger
        self.reference_catalog=None;self.reference_credentials=None;self.reference_access=None
        self.reference_error=None;self.reference_access_error=None
        self._reference_preferences_error=None
        self.collector_id='';self._reference_preferences=None;self._reference_snapshot=None
        self._reference_completed=();self._reference_reserved=();self._reference_entries=[];self._reference_active=None
        self._reference_retry=None;self._reference_reap_outcome=None;self._reference_cli_image=None;self._reference_cli_force=False
        self._reference_manual_id=None;self._reference_histories={}
        self._deferred_catalog_revision=None
        self._reference_task=dict(id='all',name='参考图列表',total=0,remaining=0,position=0,catalog_revision=0)
        self.images=[];self.selected_image=None
        self.usb_task_importer=None
        try:
            from usb_task_import import recover_replace
            recover_replace(BASE,PROJECT)
            from recording_store import RecordingStore
            self.recording_store=RecordingStore(BASE,PROJECT,ledger)
            self.recording_store.recover()
            self.reference_catalog=ReferenceCatalog(BASE,project=PROJECT)
            from task_block_sampling import TaskBlocks
            settings=self.reference_catalog.task_block_policy
            self._reference_task_blocks=TaskBlocks(BASE,settings) if settings else None
            self._base_reference_task_blocks=self._reference_task_blocks
            self.reference_catalog.import_legacy(_legacy_paths(PROJECT,ledger))
            self._reference_preferences=_preferences(BASE)
            self.collector_id=self._reference_preferences['collector_id']
            identifier=self._reference_preferences['selected_list_id']
            self._configure_text_blocks(identifier)
            if selected_image and (explicit_image or identifier=='all'):
                self._reference_cli_image=selected_image;self._reference_cli_force=True
                if not self.reference_catalog.single_list_mode:identifier='all'
                self.selected_image=selected_image
            self._reference_completed=_completed_paths(PROJECT,ledger)
            self._reference_reserved=_reserved_paths(PROJECT,ledger)
            self.refresh_reference_histories()
            snapshot=self.reference_catalog.freeze_list(identifier,self.reference_catalog.revision,include_completed=True)
            self._apply_reference_snapshot(snapshot)
        except (OSError,ValueError,RecursionError) as error:
            self.reference_error='参考图库暂不可用：'+str(error)
        try:
            self.reference_credentials=ReferenceCredentials(BASE)
            from reference_access import ReferenceAccess
            self.reference_access=ReferenceAccess(self.reference_credentials,collector_provider=lambda:self.collector_id)
        except Exception as error:
            self.reference_access_error='图片管理入口暂不可用：'+str(error)
        if self.reference_catalog is not None:
            from usb_task_import import UsbTaskImporter
            self.usb_task_importer=UsbTaskImporter(BASE,PROJECT,self.reference_catalog)

    def start_usb_export(self,recover=False):
        from usb_export_gate import pending_exports
        from project_paths import CODE_BASE
        service=self.usb_export_service
        previous=self.state
        cloud=getattr(self.dashboard,'cloud_uploads',None)
        if recover:
            if self.state!='usb_export_recovery':raise ValueError('当前没有待恢复的U盘导出')
            if cloud is not None:
                with cloud.lock:
                    if cloud.active or cloud.process:raise ValueError('请等待云端上传停止后再恢复导出')
                    self.state='usb_exporting';self.state_write()
            else:self.state='usb_exporting';self.state_write()
        else:
            privacy=self.dashboard.privacy.snapshot()
            importer=getattr(self,'usb_task_importer',None)
            if (not self.reference_can_change() or privacy.get('required') is not False or
                    privacy.get('stop_confirmed') is not True or privacy.get('phase')!='idle' or
                    (importer is not None and importer.active) or pending_exports(CODE_BASE)):
                raise ValueError('请先结束采集、任务导入和未完成的导出')
            self._usb_export_previous_state=self.state
            if cloud is not None:
                with cloud.lock:
                    if cloud.active or cloud.process:raise ValueError('请等待云端上传停止后再导出')
                    self._usb_export_cloud_paused=cloud.state['paused']
                    cloud.state['paused']=True;cloud._save()
                    self.state='usb_exporting';self.state_write()
            else:self.state='usb_exporting';self.state_write()
        try:service.start(recover=recover)
        except Exception:
            if cloud is not None and hasattr(self,'_usb_export_cloud_paused'):
                with cloud.lock:
                    cloud.state['paused']=self._usb_export_cloud_paused;cloud._save()
                del self._usb_export_cloud_paused
            self.state=previous;self.state_write();raise

    def poll_usb_export(self):
        from usb_export_gate import pending_exports
        from project_paths import CODE_BASE
        service=getattr(self,'usb_export_service',None)
        if service is None or self.state not in ('usb_exporting','usb_export_recovery') or service.active():return
        if pending_exports(CODE_BASE):
            if self.state!='usb_export_recovery':self.state='usb_export_recovery';self.state_write()
            return
        if self.state in ('usb_exporting','usb_export_recovery'):
            self.state=getattr(self,'_usb_export_previous_state','device_confirm')
            cloud=getattr(getattr(self,'dashboard',None),'cloud_uploads',None)
            if cloud is not None and hasattr(self,'_usb_export_cloud_paused'):
                with cloud.lock:
                    cloud.state['paused']=self._usb_export_cloud_paused;cloud._save()
                del self._usb_export_cloud_paused
            self.state_write()

    def start_usb_task_import(self,value):
        privacy=self.dashboard.privacy.snapshot()
        if (not self.reference_can_change() or privacy.get('required')
                or privacy.get('stop_confirmed') is not True or privacy.get('phase')!='idle'):
            raise ValueError('请先结束或放弃当前准备/采集，再整理任务库')
        if self.usb_task_importer is None:raise ValueError('U盘导入暂未就绪')
        self.usb_task_importer.start(value)
        self._usb_import_previous_state=self.state
        self.state='usb_importing';self.state_write()

    def poll_usb_task_import(self):
        manager=getattr(self,'usb_task_importer',None)
        if manager is None or self.state!='usb_importing':return
        state=manager.status()['state']
        if state=='ready':
            try:
                preferences=manager.commit()
                self._reference_preferences=preferences
                self._reference_retry=None;self._reference_manual_id=None
                self._reference_cli_image=None;self._reference_cli_force=False
                self.selected_image=None;self.image=None
                self.reference_error=None;self._reference_preferences_error=None
                self.reference_refresh(force=True)
            except Exception as error:
                pending=manager.root/'pending.json'
                if pending.exists():
                    manager.update(state='needs_recovery',error=str(error),message='任务库提交结果待恢复，暂时禁止采集：'+str(error))
                    self.state='usb_import_recovery';self.state_write();return
                else:
                    current=manager.status().get('state')
                    if current=='complete':
                        self.reference_error='任务列表已提交，但参考图进度刷新失败：'+str(error)
                    elif current!='failed':
                        manager.update(state='failed',error=str(error),message='任务库修改未完成，原有列表保留：'+str(error))
            state=manager.status()['state']
        if state in ('complete','failed'):
            self.state=getattr(self,'_usb_import_previous_state','device_confirm')
            if self.state=='exhausted' and self.images:self.state='idle'
            self.state_write()

    def _text_blocks_for(self,identifier):
        item=self.reference_catalog._load().get('lists',{}).get(identifier,{})
        ids=item.get('text_task_list_ids')
        from project_paths import COLLECTION_MODE
        from task_block_sampling import TaskBlocks,ensure_default_policy
        if ids is None:
            blocks=getattr(self,'_base_reference_task_blocks',None)
            if blocks is None and COLLECTION_MODE=='eeg':
                settings=ensure_default_policy(self._reference_base,self.reference_catalog._load())
                if settings is not None:
                    self.reference_catalog.task_block_policy=settings
                    blocks=TaskBlocks(self._reference_base,settings)
                    self._base_reference_task_blocks=blocks
            return blocks
        if COLLECTION_MODE!='eeg':raise ValueError('请先切换脑电模式，再选择临时脑电任务池')
        # Text pools already define groups of five; an unrelated image policy
        # must not silently change a scoped text pool's group size.
        return TaskBlocks(self._reference_base,dict(list_ids=list(ids),block_size=5,count_basis='saved',
            round_robin=True,namespace=item['text_task_namespace']))

    def _configure_text_blocks(self,identifier):
        self._reference_task_blocks=self._text_blocks_for(identifier)

    def reference_can_change(self):
        return (getattr(self,'state',None) in SAFE_STATES and getattr(self,'worker',None) is None
                and not getattr(self,'quit',False) and getattr(self,'reference_catalog',None) is not None
                and getattr(self,'_reference_preferences',None) is not None)

    def reference_can_switch_list(self):
        from manual_reference import can_select
        return can_select(self)

    def _select_task_block_list(self,value):
        blocks=self._reference_task_blocks
        if not self.reference_can_switch_list():raise ValueError('请先结束正式采集并处理结果，再更换列表')
        if blocks.size!='all':raise ValueError('当前分组模式仍按原规则自动切换')
        if (not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}
                or not valid_id(value['list_id']) or type(value['catalog_revision']) is not int
                or not 0<=value['catalog_revision']<=9007199254740991):raise ValueError('参考图列表选择无效')
        identifier=value['list_id']
        if identifier not in blocks.ids:raise ValueError('请选择本机场景中的具体任务列表')
        # Check the target and the full enabled scene before releasing any preparation.
        self.reference_catalog.freeze_list(identifier,value['catalog_revision'],include_completed=True)
        snapshot=self.reference_catalog.freeze_list('pool',value['catalog_revision'],include_completed=True)
        completed=_completed_paths(self._reference_project,self._reference_ledger)
        reserved=_reserved_paths(self._reference_project,self._reference_ledger)
        candidates=[item for item in snapshot['items'] if any(row['id']==identifier for row in item.get('source_lists',[]))]
        if not candidates:raise ValueError('此列表未启用，请先勾选该列表参与抽取')
        if all(item['relative_path'] in set(completed) for item in candidates):raise ValueError('这个列表已完成，请选择其他未完成列表')
        if blocks.value['current_task']==identifier:
            self._update_reference_task();self.state_write();return
        if getattr(self,'worker',None):
            path=self.path
            if path and (path/'start_gate.json').exists():raise ValueError('本段已正式开录，不能更换列表')
            self.release_prepared()
            if path and path.is_dir():self.recording_store.abandon_preparation(path)
        preferences=dict(self._reference_preferences,selected_list_id='pool')
        if preferences!=self._reference_preferences:_save_preferences(self._reference_base,preferences)
        try:blocks.switch_task(identifier,snapshot,completed)
        except (OSError,ValueError) as error:
            self.reference_error='任务列表切换未确认保存，请重新选择：'+str(error);raise
        self._reference_preferences=preferences;self._reference_completed=completed;self._reference_reserved=reserved
        self._reference_retry=None;self._reference_manual_id=None;self._reference_cli_image=None;self._reference_cli_force=False
        self._reference_active=None;self._deferred_catalog_revision=None;self.selected_image=None
        self.path=None;self.image=None;self.reference_error=None;self._reference_preferences_error=None
        self.refresh_reference_histories();self._apply_reference_snapshot(snapshot)
        if self.state!='device_confirm':self.state='idle'
        self.state_write()

    def reference_completed_paths(self):
        return getattr(self,'_reference_completed',())

    def reference_task_state(self):
        value=dict(getattr(self,'_reference_task',{}))
        value['can_change']=self.reference_can_change()
        value['can_switch']=self.reference_can_switch_list()
        return value

    def _task_block_owner_recovery_safe(self):
        return (getattr(self,'scan_cleanup_pending',None) is False
                and getattr(self,'owner_reconcile_pending',None) is False
                and hasattr(self,'pending_camera_owner') and self.pending_camera_owner is None)

    def task_block_settings(self):
        """Publish policy evidence and the same eligibility used by the setter."""
        from project_paths import BASE,COLLECTION_CONTEXT
        blocks=getattr(self,'_reference_task_blocks',None)
        result=dict(enabled=False,configured_size=None,current_target=None,policy_revision=None,
                     can_change=False,reason='任务分组设置写入尚未启用')
        if blocks is None or Path(getattr(self,'_reference_base',''))!=Path(BASE):return result
        if getattr(blocks,'round_robin',False):
            return dict(result,enabled=True,configured_size=blocks.size,current_target=blocks.value['target_size'],
                count_basis=blocks.count_basis,reason='文字任务池独立保存进度，按保存条数自动轮换')
        path=Path(BASE)/'configs/task_block_policy.json'
        try:
            fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size>1048576:raise ValueError('无效的任务分组配置')
                raw=os.read(fd,1048577)
            finally:os.close(fd)
            if len(raw)>1048576:raise ValueError('任务分组配置过大')
            value=unique_json(raw)
            if (not isinstance(value,dict) or set(value) not in ({'version','list_ids','block_size'},{'version','list_ids','block_size','count_basis'})
                    or type(value['version']) is not int or value['version']!=1
                    or value['list_ids']!=blocks.ids or value['block_size']!=blocks.size
                    or value.get('count_basis','saved')!=getattr(blocks,'count_basis','saved')
                    or not (value['block_size']=='all' or (type(value['block_size']) is int and 1<=value['block_size']<=5000))):
                raise ValueError('任务分组配置已变化')
            result.update(enabled=True,configured_size=value['block_size'],current_target=blocks.value['target_size'],
                          count_basis=getattr(blocks,'count_basis','saved'),policy_revision=hashlib.sha256(raw).hexdigest())
            try:
                self._task_block_write_check(COLLECTION_CONTEXT,getattr(self,'ui_revision',None),result['policy_revision'],None)
                result.update(can_change=True,reason=None)
            except Exception as error:
                result['reason']=str(error) or '当前状态不允许修改'
        except (OSError,ValueError,TypeError,KeyError):
            result['reason']='任务分组配置不可读取或与当前状态不一致'
        return result

    def _task_block_write_check(self, expected_context, expected_revision, expected_policy_revision, job):
        from project_paths import BASE,COLLECTION_MODE,COLLECTION_CONTEXT
        if (COLLECTION_MODE!='eeg' or not isinstance(expected_context,str) or not expected_context
                or expected_context!=COLLECTION_CONTEXT or Path(self._reference_base)!=Path(BASE)
                or type(expected_revision) is not int or type(getattr(self,'ui_revision',None)) is not int
                or expected_revision!=self.ui_revision):raise ValueError('采集上下文或流程已变化')
        required=('worker','capture_owner_run','capture_registry_cleanup_pending','_reference_active',
                  '_reference_retry','_reference_reserved','_reference_task_blocks','reference_catalog',
                  '_reference_preferences','state','quit')
        if any(not hasattr(self,k) for k in required):raise ValueError('采集状态未知')
        if not self._task_block_owner_recovery_safe():raise ValueError('设备所有权清理或恢复未确认')
        blocks=self._reference_task_blocks
        if (self.state not in SAFE_STATES or self.quit or getattr(self,'switch_requested',False)
                or self.worker is not None or self.capture_owner_run is not None
                or self.capture_registry_cleanup_pending is not False
                or self._reference_active is not None or self._reference_retry is not None
                or not isinstance(self._reference_reserved,(tuple,list))
                or (self._reference_reserved and getattr(blocks,'count_basis','saved')!='saved')
                or getattr(self,'_reference_transaction',None) is not None
                or blocks is None or self.reference_catalog is None or self._reference_preferences is None):
            raise ValueError('采集或恢复状态不允许修改')
        if (blocks.value['pending'] is not None or blocks.unresolved()
                or (blocks.awaiting_qc() and getattr(blocks,'count_basis','saved')!='saved')):
            raise ValueError('任务分组仍有待处理记录')
        dashboard=getattr(self,'dashboard',None)
        if dashboard is None:raise ValueError('控制台不可用')
        privacy=dashboard.privacy.snapshot()
        if (not isinstance(privacy,dict) or privacy.get('required') is not False
                or privacy.get('stop_confirmed') is not True or privacy.get('phase')!='idle'):
            raise ValueError('采集隐私状态未确认安全')
        if job is None:
            with dashboard.lock:
                if dashboard.busy or dashboard.pending is not None:raise ValueError('控制台正忙')
        else:
            if (not isinstance(job,dict) or job.get('value')!=job.get('requested_value')
                    or job.get('collection_context')!=expected_context
                    or job.get('submitted_revision')!=expected_revision
                    or job.get('revision')!=expected_revision
                    or job.get('policy_revision')!=expected_policy_revision
                    or not dashboard.execution_matches(job)):
                raise ValueError('任务分组执行凭证无效')
        policy,revision=_task_block_policy(BASE)
        if (revision!=expected_policy_revision or policy['list_ids']!=blocks.ids
                or policy['block_size']!=blocks.size
                or policy.get('count_basis','saved')!=getattr(blocks,'count_basis','saved')
                or self.reference_catalog.task_block_policy!=policy):
            raise ValueError('任务分组配置已变化')
        return Path(BASE),policy

    def set_task_block_size(self,value,*,expected_context,expected_revision,expected_policy_revision,job=None):
        """Persist next-block size on the controller actor; never dispatches a job."""
        self._assert_controller_thread()
        result=dict(ok=False,outcome='rejected',error=None,readback=None,settings=None)
        if type(value) is not int or not 1<=value<=5000:
            result['error']='每任务参考图数应为 1 到 5000 的整数';return result
        if job is not None and (not isinstance(job,dict) or job.get('requested_value')!=value):
            result['error']='任务分组执行凭证无效';return result
        try:
            base,policy=self._task_block_write_check(expected_context,expected_revision,expected_policy_revision,job)
            self._task_block_write_active=True
            try:
                base,policy=self._task_block_write_check(expected_context,expected_revision,expected_policy_revision,job)
                candidate=dict(policy,block_size=value)
                try:
                    _durable(base/'configs/task_block_policy.json',_encoded(candidate))
                    outcome='applied';error=None
                except Exception as exc:
                    outcome=('write_failed' if (isinstance(exc,CatalogError) and exc.committed is False) or isinstance(exc,OSError)
                             else 'uncertain')
                    error=str(exc)
                from project_paths import BASE,COLLECTION_MODE,COLLECTION_CONTEXT
                if Path(BASE)!=base or COLLECTION_MODE!='eeg' or COLLECTION_CONTEXT!=expected_context:
                    result.update(outcome='uncertain' if outcome!='write_failed' else outcome,error='采集上下文已变化')
                    return result
                try:
                    actual,revision=_task_block_policy(base)
                    if actual['list_ids']!=policy['list_ids']:raise ValueError('任务范围已变化')
                    self._reference_task_blocks.size=actual['block_size']
                    self.reference_catalog.task_block_policy=actual
                    result['readback']=dict(configured_size=actual['block_size'],policy_revision=revision)
                    result['settings']=self.task_block_settings()
                    if outcome=='applied' and actual!=candidate:
                        outcome='uncertain';error='写后配置读回不匹配'
                except Exception as exc:
                    if outcome=='applied':outcome='uncertain'
                    error='配置读回失败：'+str(exc)
                result.update(ok=outcome=='applied',outcome=outcome,error=error)
                return result
            finally:self._task_block_write_active=False
        except Exception as exc:
            result['error']=str(exc);return result

    def reference_image_title(self):
        active=getattr(self,'_reference_active',None)
        if active and active.get('selection'):
            return active['selection']['asset']['name']
        image=getattr(self,'image',None)
        if image:
            for item in (getattr(self,'_reference_snapshot',None) or {}).get('items',[]):
                if Path(self._reference_project)/item['relative_path']==image:return item['name']
        return image.stem.replace('_','，') if image else '本段任务'

    def refresh_reference_histories(self):
        result={};cache={}
        for image in self._reference_ledger.images():
            image=Path(image);path=image.parent/'used.yaml'
            if path not in cache:cache[path]=self._reference_ledger.read(path)
            document=cache[path];entry=document.get('used',{}).get(image.name)
            complete=entry is not None and self._reference_ledger.valid_used(entry,image)
            attempts=[v for v in document.get('attempts',{}).get(image.name,[]) if isinstance(v,dict) and v.get('has_data')]
            latest=attempts[-1] if attempts else None
            pending=entry is not None and self._reference_ledger.reserved_used(entry,image)
            state='pending_qc' if pending else 'collected' if complete else 'unattempted' if not attempts else 'attempted_uncollected'
            result[image.relative_to(self._reference_project).as_posix()]=dict(state=state,ever_recorded=bool(attempts),attempt_count=len(attempts),
                collected=complete and not pending,pending_qc=pending,latest_qc=latest.get('qc_level') if latest else None,
                latest_deleted=bool(latest and latest.get('deleted')),latest_retained=bool(latest and latest.get('retained')),
                history=attempts[-5:])
        self._reference_histories=result

    def reference_history_states(self):return getattr(self,'_reference_histories',{})

    def reference_image_state(self):
        image=getattr(self,'image',None)
        if not image or not getattr(self,'_reference_project',None):return {}
        relative=Path(image).relative_to(self._reference_project).as_posix()
        item=next((v for v in (getattr(self,'_reference_snapshot',None) or {}).get('items',[]) if v['relative_path']==relative),None)
        value=dict(getattr(self,'_reference_histories',{}).get(relative,dict(state='unattempted',ever_recorded=False,attempt_count=0,collected=False)),
                    id=item['id'] if item else None,name=item['name'] if item else Path(image).stem,
                    manual=bool(item and item['id']==getattr(self,'_reference_manual_id',None)),kind=item.get('kind','image') if item else 'image')
        path=getattr(self,'path',None)
        if (getattr(self,'worker',None) is not None and getattr(self,'state','') in ('recording','confirm_stop','finalizing','error')
                and path and (path/'start_gate.json').is_file()):
            status=self.worker_status();value.update(pending_current_result=True,ever_recorded=True,
                attempt_count=value['attempt_count']+1,current_qc='ERROR' if status.get('state') in ('error','qc_error') else status.get('qc_level'))
        return value

    def _reference_entry_choice(self,entries):
        blocks=getattr(self,'_reference_task_blocks',None)
        if blocks:return blocks.choose(entries,self._reference_image_choice)
        return self._reference_image_choice(entries)

    def _reference_image_choice(self,entries):
        histories=getattr(self,'_reference_histories',{})
        fresh=[entry for entry in entries if not histories.get(entry['path'].relative_to(self._reference_project).as_posix(),{}).get('ever_recorded')]
        errors=[entry for entry in entries if histories.get(entry['path'].relative_to(self._reference_project).as_posix(),{}).get('latest_qc')=='ERROR']
        return secrets.choice(fresh or errors or entries)

    def select_reference_image(self,value=None,next_image=False):
        if getattr(getattr(self,'_reference_task_blocks',None),'round_robin',False):raise ValueError('临时脑电任务按保存条数轮换，不能手动跳过当前任务')
        if getattr(self,'_reference_transaction',None) is not None:
            raise ValueError('参考图事务恢复未确认，请保留本段并联系维护人员')
        if getattr(self,'_reference_task_blocks',None):
            from manual_reference import choose
            return choose(self,value,next_image=next_image)
        if self.state not in ('device_confirm','idle','failed_wait','exhausted','ready'):raise ValueError('请等待准备完成后再选择参考图')
        if getattr(self,'_reference_task_blocks',None):
            raise ValueError('请完成当前任务后自动切换；本任务内图片会随机且不重复')
        if self.dashboard.privacy.snapshot().get('required'):raise ValueError('采集状态未确认停止')
        snapshot=self.reference_catalog.freeze_list(self._reference_preferences['selected_list_id'],
            self.reference_catalog.revision if next_image else value['catalog_revision'],include_completed=True)
        if not next_image and not any(item['id']==value['image_id'] for item in snapshot['items']):raise ValueError('参考图不在当前任务列表中')
        previous=getattr(self,'image',None)
        if getattr(self,'worker',None):
            if self.path and (self.path/'start_gate.json').exists():raise ValueError('本段已开录，请先完成结果处理')
            path=self.path;self.release_prepared()
            if path and path.is_dir():self.recording_store.abandon_preparation(path)
        self._reference_retry=None;self._reference_manual_id=None;self._reference_cli_image=None;self._reference_cli_force=False
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger)
        self._reference_reserved=_reserved_paths(self._reference_project,self._reference_ledger)
        self.refresh_reference_histories();self._apply_reference_snapshot(snapshot)
        if next_image:
            entries=[entry for entry in self._reference_entries if entry['path']!=previous] or self._reference_entries
            if not entries:raise ValueError('当前列表没有待采集参考图，可手动选择已采集图重新采集')
            entry=self._reference_entry_choice(entries);identifier=entry['snapshot']['items'][entry['index']]['id']
        else:
            identifier=value['image_id']
            selected=next((item for item in snapshot['items'] if item['id']==identifier),None)
            if selected and selected['relative_path'] in set(self._reference_reserved):
                raise ValueError('该参考图等待完整质检，暂不能重复抽取')
        self._reference_manual_id=identifier;self._apply_reference_snapshot(snapshot)
        entry=next(entry for entry in self._reference_entries if entry['snapshot']['items'][entry['index']]['id']==identifier)
        self.image=entry['path']
        prefetch=getattr(self,'prefetch_reference_announcement',None)
        if callable(prefetch):prefetch()
        self.state_write()
        if self.state!='device_confirm' and self.selected_devices:self.state='idle';self.prepare()

    def _apply_reference_snapshot(self,snapshot):
        retry=self._reference_retry
        if retry and retry['snapshot']['list_id']==snapshot['list_id']:
            failed_id=retry['snapshot']['items'][retry['index']]['id']
            if not any(item['id']==failed_id for item in snapshot['items']):
                # An explicit removal/deletion abandons this failed reference.
                self._reference_retry=None;retry=None;self._deferred_catalog_revision=None
            else:
                # A retry is the same segment, not a mixture of two plans.
                # Keep its whole prior plan until success or explicit Apply.
                self._deferred_catalog_revision=(snapshot['catalog_revision']
                    if snapshot['catalog_revision']!=retry['snapshot']['catalog_revision'] else None)
                snapshot=retry['snapshot']
        else:
            self._deferred_catalog_revision=None
        completed=set(self._reference_completed);reserved=set(getattr(self,'_reference_reserved',()))
        first=_first_image_index(snapshot,self._reference_cli_image,self._reference_project)
        entries=[dict(snapshot=snapshot,index=index,path=self._reference_project/item['relative_path'])
                 for index,item in enumerate(snapshot['items']) if (first is None or index>=first)
                 and item['relative_path'] not in reserved
                 and ((index==first and self._reference_cli_force) or item['relative_path'] not in completed or item['id']==getattr(self,'_reference_manual_id',None))]
        if retry and retry['snapshot']['list_id']==snapshot['list_id']:
            entries=[retry]+[entry for entry in entries if entry['index']!=retry['index']]
        blocks=getattr(self,'_reference_task_blocks',None)
        if blocks:
            blocks.reconcile(snapshot,self._reference_completed,getattr(self,'_reference_reserved',()))
            # TaskBlocks owns round selection, including the next round.
        self._reference_snapshot=snapshot;self._reference_entries=entries
        self.images=[entry['path'] for entry in entries]
        self._update_reference_task()

    def _update_reference_task(self):
        active=getattr(self,'_reference_active',None)
        entries=getattr(self,'_reference_entries',[])
        manual=next((v for v in entries if v['snapshot']['items'][v['index']]['id']==getattr(self,'_reference_manual_id',None)),None)
        entry=active.get('entry') if active else manual or (entries[0] if entries else None)
        blocks=getattr(self,'_reference_task_blocks',None)
        if blocks and not active and blocks.value['current_task']:
            entry=next((v for v in entries if blocks.task(v)==blocks.value['current_task']),entry)
        snapshot=entry['snapshot'] if entry else getattr(self,'_reference_snapshot',None)
        if snapshot is None:return
        self._reference_task=dict(id=snapshot['list_id'],name=snapshot['list_name'],
            total=snapshot['total'],remaining=len(self.images),position=entry['index']+1 if entry else 0,
            catalog_revision=snapshot['catalog_revision'],selection_mode='random_without_replacement')
        if getattr(self.reference_catalog,'single_list_mode',False):
            self._reference_task['selection_scope']='single_list'
        blocks=getattr(self,'_reference_task_blocks',None)
        if blocks:self._reference_task.update(blocks.status())
        if entry:
            asset=snapshot['items'][entry['index']]
            self._reference_task.update(tags=asset.get('tags', []), source_lists=asset.get('source_lists', []))
            if blocks and blocks.value['current_task']:
                source=next((v for v in asset.get('source_lists',[]) if v['id']==blocks.value['current_task']),None)
                if source:
                    done=blocks.status()['block_completed'];target=blocks.value['target_size']
                    self._reference_task.update(name=source['name'],total=target,remaining=target-done,position=done+1)
        if not blocks and getattr(self,'_reference_manual_id',None):
            self._reference_task['remaining']=sum(v['path'].relative_to(self._reference_project).as_posix() not in set(self._reference_completed) for v in entries)

    def reference_refresh(self,force=False):
        if getattr(self,'_reference_transaction',None) is not None:
            raise ValueError('参考图切换恢复未确认，请保留本段并联系维护人员')
        if getattr(self,'reference_catalog',None) is None or getattr(self,'_reference_preferences',None) is None:
            return False
        current=self._reference_snapshot
        if not force and current is not None and self.reference_catalog.revision==current['catalog_revision']:
            return False
        if (not force and self._reference_retry is not None
                and self.reference_catalog.revision==getattr(self,'_deferred_catalog_revision',None)):
            return False
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger)
        self._reference_reserved=_reserved_paths(self._reference_project,self._reference_ledger)
        self.refresh_reference_histories()
        identifier=current['list_id'] if self._reference_cli_image and current else self._reference_preferences['selected_list_id']
        if getattr(self,'_reference_task_blocks',None) is None:self._configure_text_blocks(identifier)
        try:snapshot=self.reference_catalog.freeze_list(identifier,self.reference_catalog.revision,include_completed=True)
        except CatalogError as error:
            if error.code not in ('hidden_list','deleted','not_found'):raise
            if self.reference_catalog.single_list_mode:
                raise CatalogError('当前列表不可用，请手动选择其他任务列表','list_selection_required',409) from error
            preferences=dict(self._reference_preferences,selected_list_id='pool')
            _save_preferences(self._reference_base,preferences);self._reference_preferences=preferences
            self._reference_retry=None;self._reference_manual_id=None
            snapshot=self.reference_catalog.freeze_list('pool',self.reference_catalog.revision,include_completed=True)
            self._reference_task_blocks=self._text_blocks_for(snapshot['list_id'])
        self._apply_reference_snapshot(snapshot);self.reference_error=self._reference_preferences_error
        return True

    def reference_tick(self):
        self.poll_usb_export()
        self.poll_usb_task_import()
        if not self.reference_can_change():return
        try:
            signal=self._reference_base/'runtime/qc_results_revision.json'
            try:revision=signal.stat().st_mtime_ns
            except FileNotFoundError:revision=None
            qc_changed=revision!=getattr(self,'_qc_results_revision',None)
            changed=self.reference_refresh(force=qc_changed)
            self._qc_results_revision=revision
            if changed:
                if self.state in ('idle','exhausted'):self.state='idle' if self.images else 'exhausted'
                self.state_write()
        except (ValueError,OSError,RecursionError) as error:
            message='参考图列表已变化，请重新选择：'+str(error)
            if message!=self.reference_error:
                self.reference_error=message
                try:self.state_write()
                except (ValueError,OSError):pass

    def select_reference_list(self,value):
        if getattr(self,'_reference_transaction',None) is not None:
            raise ValueError('参考图事务恢复未确认，请保留本段并联系维护人员')
        blocks=getattr(self,'_reference_task_blocks',None)
        requested=value.get('list_id') if isinstance(value,dict) else None
        target=self.reference_catalog._load().get('lists',{}).get(requested,{}) if self.reference_catalog is not None else {}
        if blocks and not getattr(blocks,'round_robin',False) and not target.get('text_task_list_ids'):
            from manual_reference import choose
            return choose(self,value,list_only=True)
        if not self.reference_can_change():raise ValueError('当前正在准备或采集，结束本段后再选择列表')
        if (not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}
                or not valid_id(value['list_id']) or type(value['catalog_revision']) is not int
                or not 0<=value['catalog_revision']<=9007199254740991):
            raise ValueError('参考图列表选择无效')
        snapshot=self.reference_catalog.freeze_list(value['list_id'],value['catalog_revision'],include_completed=True)
        next_blocks=self._text_blocks_for(value['list_id'])
        preferences=dict(self._reference_preferences,selected_list_id=snapshot['list_id'])
        try:_save_preferences(self._reference_base,preferences)
        except (OSError,ValueError) as error:
            self.reference_error='列表选择未保存，请重试：'+str(error)
            self._reference_preferences_error=self.reference_error
            raise
        self._reference_task_blocks=next_blocks
        self._reference_preferences=preferences;self._reference_cli_image=None;self._reference_cli_force=False;self.selected_image=None
        self._reference_retry=None;self._deferred_catalog_revision=None
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger)
        self._reference_reserved=_reserved_paths(self._reference_project,self._reference_ledger)
        self._apply_reference_snapshot(snapshot);self._reference_preferences_error=None;self.reference_error=None
        if self.state=='exhausted' and self.images:self.state='idle'
        self.state_write()

    def reference_begin_preparation(self):
        self._reference_mutation_started()
        if self._reference_active is not None:
            raise ValueError('当前参考图仍在准备或采集中，请结束本段后再抽取')
        self.reference_refresh()
        if self.reference_error:raise ValueError(self.reference_error)
        if not self._reference_entries:raise ValueError('当前列表没有待采集参考图')
        # Draw once per new segment. A failed/retried segment keeps its frozen
        # image; the original snapshot/index/hash remain valid for existing QC.
        manual=next((entry for entry in self._reference_entries if entry['snapshot']['items'][entry['index']]['id']==getattr(self,'_reference_manual_id',None)),None)
        entry=(manual if manual is not None else self._reference_retry if self._reference_retry is not None else
               self._reference_entries[0] if self._reference_cli_force else
               self._reference_entry_choice(self._reference_entries))
        item=entry['snapshot']['items'][entry['index']]
        owner=uuid.uuid4().hex
        self.reference_catalog.pin(item['id'],owner)
        active=dict(owner=owner,entry=entry,path=None,identity=None,selection=None,raw_meta=None)
        self._reference_active=active
        try:
            selection=self.reference_catalog.per_item_selection(entry['snapshot'],entry['index'],owner_token=owner)
            path=self.reference_catalog.asset_path(item['id'])
            if path.relative_to(self._reference_project).as_posix()!=selection['asset']['relative_path']:
                raise ValueError('准备图像来源与冻结凭据不同')
            self.image=path;active['selection']=selection;self._update_reference_task()
            return selection
        except Exception:
            self.reference_release_pin();raise

    def reference_reserve_directory(self,root):
        deadline=time.monotonic()+3
        while True:
            try:
                path,identity=reserve_session_dir(root,self.collector_id)
                active=self._reference_active;active['path']=path;active['identity']=identity
                try:active['scene_snapshot']=self.reference_catalog.snapshot_scene(active['entry']['snapshot'],path,active['entry']['index'])
                except CatalogError as error:error.path=path;raise
                return path,dict(identity)
            except AllocationDurabilityError as error:
                self._reference_active['path']=error.path;self._reference_active['identity']=error.identity
                self.path=error.path
                raise
            except FileExistsError:
                if time.monotonic()>=deadline:raise ValueError('会话时间重名，请校准系统时间后重试')
                time.sleep(.1)

    def reference_set_raw_metadata(self,value):
        if self._reference_active:
            selection=self._reference_active.get('selection') or {}
            if (selection.get('asset') or {}).get('kind')=='text':
                import hashlib
                from text_reference import read
                raw=Path(self.image).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=selection['asset']['sha256']:raise ValueError('文字任务内容已变化')
                payload=read(raw)
                value.update(reference_kind='text',reference_text=payload['reference_text'],reference_text_payload=payload)
            if self._reference_active.get('scene_snapshot'):
                value['reference_bundle']=copy.deepcopy(self._reference_active['scene_snapshot'])
                value['reference_layout_status']='bundle_snapshot'
            self._reference_active['raw_meta']=copy.deepcopy(value)

    def reference_session_metadata(self):
        active=getattr(self,'_reference_active',None)
        if not active or active.get('path')!=getattr(self,'path',None):return {}
        value=dict(active.get('identity') or {})
        selection=active.get('selection')
        if selection:
            value['reference_selection']=copy.deepcopy(selection)
            if selection['asset'].get('kind')=='text':
                raw=active.get('raw_meta') or {}
                for key in ('reference_kind','reference_text','reference_text_payload'):
                    if key not in raw:raise ValueError('文字任务冻结元数据缺失')
                    value[key]=copy.deepcopy(raw[key])
            if active.get('scene_snapshot'):
                value['reference_bundle']=copy.deepcopy(active['scene_snapshot'])
                value['reference_layout_status']='bundle_snapshot'
            if selection['asset']['origin']=='upload':
                value.update(environment=None,scene=None,objects=None,notes=None,
                    task_name=selection['asset']['name'],reference_layout_status='bundle_snapshot' if active.get('scene_snapshot') else 'not_provided')
        return value

    def reference_release_pin(self):
        active=getattr(self,'_reference_active',None)
        if active and getattr(self,'reference_catalog',None):
            try:self.reference_catalog.unpin(active['owner'])
            except Exception as error:self.reference_error='参考图锁未能解除，请联系维护人员：'+str(error)

    def reference_prepare_failed(self,error,path=None):
        self._reference_mutation_started()
        active=getattr(self,'_reference_active',None)
        if active:self._reference_retry=active['entry']
        self.reference_release_pin();self._reference_active=None
        message='本段准备失败：'+str(error)
        self.state='failed_wait';self.last_help=[message]
        self._update_reference_task()
        try:self.state_write()
        except (OSError,ValueError):pass
        try:self.voice.say(message+'。处理后可重新准备，已有目录和数据保留。')
        except Exception:pass  # Advisory voice must not turn a recoverable prepare failure fatal.

    def reference_reaped(self):
        self._reference_mutation_started()
        active=getattr(self,'_reference_active',None)
        outcome=getattr(self,'_reference_reap_outcome',None);self._reference_reap_outcome=None
        if outcome in ('success','pending_qc'):self._reference_cli_force=False;self._reference_manual_id=None
        if active and outcome=='success':
            blocks=getattr(self,'_reference_task_blocks',None)
            if blocks:blocks.success(active['entry'])
        elif active and outcome=='pending_qc':
            blocks=getattr(self,'_reference_task_blocks',None)
            if blocks:blocks.defer(active['entry'])
        if active:
            self._reference_retry=None if outcome in ('success','pending_qc') or getattr(self,'quit',False) else active['entry']
        self.reference_release_pin();self._reference_active=None
        try:self.reference_refresh(force=True)
        except (OSError,ValueError,RecursionError) as error:self.reference_error='参考图进度暂未更新：'+str(error)

    def reference_close(self):
        self._reference_mutation_started()
        manager=getattr(self,'usb_task_importer',None)
        if manager is not None:manager.close()
        worker=getattr(self,'worker',None)
        if worker is None or worker.poll() is not None:
            self.reference_release_pin();self._reference_active=None
        access=getattr(self,'reference_access',None)
        close=getattr(access,'close',None)
        if callable(close):
            try:close()
            except Exception as error:self.reference_access_error=str(error)
