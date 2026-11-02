"""Controller-owned reference queue, immutable episode proof and list selection.

No hardware access. Catalog mutations share the exact store passed to Dashboard.
Heartbeat/status methods only read cached values. Hashing occurs at prepare.
"""
import copy
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
import uuid

from reference_catalog import ReferenceCatalog, CatalogError, unique_json, valid_id
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
        if entry is not None and ledger.valid_used(entry):
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
    identifier='all' if selected else preferences['selected_list_id']
    snapshot=catalog.freeze_list(identifier,catalog.revision,include_completed=True)
    completed=set(_completed_paths(project,ledger));first=_first_image_index(snapshot,selected,project)
    return [Path(project)/item['relative_path'] for index,item in enumerate(snapshot['items'])
            if (first is None or index>=first) and
               (index==first or item['relative_path'] not in completed)]


class ReferenceControlsMixin:
    def init_references(self,selected_image=None,explicit_image=False):
        from project_paths import BASE,PROJECT
        import ledger
        self._reference_base=Path(BASE);self._reference_project=Path(PROJECT);self._reference_ledger=ledger
        self.reference_catalog=None;self.reference_credentials=None;self.reference_access=None
        self.reference_error=None;self.reference_access_error=None
        self._reference_preferences_error=None
        self.collector_id='';self._reference_preferences=None;self._reference_snapshot=None
        self._reference_completed=();self._reference_entries=[];self._reference_active=None
        self._reference_retry=None;self._reference_reap_outcome=None;self._reference_cli_image=None;self._reference_cli_force=False
        self._reference_manual_id=None;self._reference_histories={}
        self._deferred_catalog_revision=None
        self._reference_task=dict(id='all',name='参考图列表',total=0,remaining=0,position=0,catalog_revision=0)
        self.images=[];self.selected_image=None
        try:
            from recording_store import RecordingStore
            self.recording_store=RecordingStore(BASE,PROJECT,ledger)
            self.recording_store.recover()
            self.reference_catalog=ReferenceCatalog(BASE,project=PROJECT)
            self.reference_catalog.import_legacy(_legacy_paths(PROJECT,ledger))
            self._reference_preferences=_preferences(BASE)
            self.collector_id=self._reference_preferences['collector_id']
            identifier=self._reference_preferences['selected_list_id']
            if selected_image and (explicit_image or identifier=='all'):
                self._reference_cli_image=selected_image;self._reference_cli_force=True;identifier='all';self.selected_image=selected_image
            self._reference_completed=_completed_paths(PROJECT,ledger)
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

    def reference_can_change(self):
        return (getattr(self,'state',None) in SAFE_STATES and getattr(self,'worker',None) is None
                and not getattr(self,'quit',False) and getattr(self,'reference_catalog',None) is not None
                and getattr(self,'_reference_preferences',None) is not None)

    def reference_completed_paths(self):
        return getattr(self,'_reference_completed',())

    def reference_task_state(self):
        value=dict(getattr(self,'_reference_task',{}))
        value['can_change']=self.reference_can_change()
        return value

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
            complete=entry is not None and self._reference_ledger.valid_used(entry)
            attempts=[v for v in document.get('attempts',{}).get(image.name,[]) if isinstance(v,dict) and v.get('has_data')]
            latest=attempts[-1] if attempts else None
            state='collected' if complete else 'unattempted' if not attempts else 'attempted_uncollected'
            result[image.relative_to(self._reference_project).as_posix()]=dict(state=state,ever_recorded=bool(attempts),attempt_count=len(attempts),
                collected=complete,latest_qc=latest.get('qc_level') if latest else None,
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
                    manual=bool(item and item['id']==getattr(self,'_reference_manual_id',None)))
        path=getattr(self,'path',None)
        if (getattr(self,'worker',None) is not None and getattr(self,'state','') in ('recording','confirm_stop','finalizing','error')
                and path and (path/'start_gate.json').is_file()):
            status=self.worker_status();value.update(pending_current_result=True,ever_recorded=True,
                attempt_count=value['attempt_count']+1,current_qc='ERROR' if status.get('state') in ('error','qc_error') else status.get('qc_level'))
        return value

    def _reference_entry_choice(self,entries):
        histories=getattr(self,'_reference_histories',{})
        fresh=[entry for entry in entries if not histories.get(entry['path'].relative_to(self._reference_project).as_posix(),{}).get('ever_recorded')]
        errors=[entry for entry in entries if histories.get(entry['path'].relative_to(self._reference_project).as_posix(),{}).get('latest_qc')=='ERROR']
        return secrets.choice(fresh or errors or entries)

    def select_reference_image(self,value=None,next_image=False):
        if self.state not in ('device_confirm','idle','failed_wait','exhausted','ready'):raise ValueError('请等待准备完成后再选择参考图')
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
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger);self.refresh_reference_histories();self._apply_reference_snapshot(snapshot)
        if next_image:
            entries=[entry for entry in self._reference_entries if entry['path']!=previous] or self._reference_entries
            if not entries:raise ValueError('当前列表没有待采集参考图，可手动选择已采集图重新采集')
            entry=self._reference_entry_choice(entries);identifier=entry['snapshot']['items'][entry['index']]['id']
        else:identifier=value['image_id']
        self._reference_manual_id=identifier;self._apply_reference_snapshot(snapshot)
        entry=next(entry for entry in self._reference_entries if entry['snapshot']['items'][entry['index']]['id']==identifier)
        self.image=entry['path'];self.state_write()
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
        completed=set(self._reference_completed);first=_first_image_index(snapshot,self._reference_cli_image,self._reference_project)
        entries=[dict(snapshot=snapshot,index=index,path=self._reference_project/item['relative_path'])
                 for index,item in enumerate(snapshot['items']) if (first is None or index>=first)
                 and ((index==first and self._reference_cli_force) or item['relative_path'] not in completed or item['id']==getattr(self,'_reference_manual_id',None))]
        if retry and retry['snapshot']['list_id']==snapshot['list_id']:
            entries=[retry]+[entry for entry in entries if entry['index']!=retry['index']]
        self._reference_snapshot=snapshot;self._reference_entries=entries
        self.images=[entry['path'] for entry in entries]
        self._update_reference_task()

    def _update_reference_task(self):
        active=getattr(self,'_reference_active',None)
        entries=getattr(self,'_reference_entries',[])
        manual=next((v for v in entries if v['snapshot']['items'][v['index']]['id']==getattr(self,'_reference_manual_id',None)),None)
        entry=active.get('entry') if active else manual or (entries[0] if entries else None)
        snapshot=entry['snapshot'] if entry else getattr(self,'_reference_snapshot',None)
        if snapshot is None:return
        self._reference_task=dict(id=snapshot['list_id'],name=snapshot['list_name'],
            total=snapshot['total'],remaining=len(self.images),position=entry['index']+1 if entry else 0,
            catalog_revision=snapshot['catalog_revision'],selection_mode='random_without_replacement')
        if getattr(self,'_reference_manual_id',None):
            self._reference_task['remaining']=sum(v['path'].relative_to(self._reference_project).as_posix() not in set(self._reference_completed) for v in entries)

    def reference_refresh(self,force=False):
        if getattr(self,'reference_catalog',None) is None or getattr(self,'_reference_preferences',None) is None:
            return False
        current=self._reference_snapshot
        if not force and current is not None and self.reference_catalog.revision==current['catalog_revision']:
            return False
        if (not force and self._reference_retry is not None
                and self.reference_catalog.revision==getattr(self,'_deferred_catalog_revision',None)):
            return False
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger)
        self.refresh_reference_histories()
        identifier=current['list_id'] if self._reference_cli_image and current else self._reference_preferences['selected_list_id']
        snapshot=self.reference_catalog.freeze_list(identifier,self.reference_catalog.revision,include_completed=True)
        self._apply_reference_snapshot(snapshot);self.reference_error=self._reference_preferences_error
        return True

    def reference_tick(self):
        if not self.reference_can_change():return
        try:
            changed=self.reference_refresh()
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
        if not self.reference_can_change():raise ValueError('当前正在准备或采集，结束本段后再选择列表')
        if (not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}
                or not valid_id(value['list_id']) or type(value['catalog_revision']) is not int
                or not 0<=value['catalog_revision']<=9007199254740991):
            raise ValueError('参考图列表选择无效')
        snapshot=self.reference_catalog.freeze_list(value['list_id'],value['catalog_revision'],include_completed=True)
        preferences=dict(self._reference_preferences,selected_list_id=snapshot['list_id'])
        try:_save_preferences(self._reference_base,preferences)
        except (OSError,ValueError) as error:
            self.reference_error='列表选择未保存，请重试：'+str(error)
            self._reference_preferences_error=self.reference_error
            raise
        self._reference_preferences=preferences;self._reference_cli_image=None;self._reference_cli_force=False;self.selected_image=None
        self._reference_retry=None;self._deferred_catalog_revision=None
        self._reference_completed=_completed_paths(self._reference_project,self._reference_ledger)
        self._apply_reference_snapshot(snapshot);self._reference_preferences_error=None;self.reference_error=None
        if self.state=='exhausted' and self.images:self.state='idle'
        self.state_write()

    def reference_begin_preparation(self):
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
                try:active['scene_snapshot']=self.reference_catalog.snapshot_scene(active['entry']['snapshot'],path) if active['entry']['snapshot'].get('scene') else None
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
        active=getattr(self,'_reference_active',None)
        outcome=getattr(self,'_reference_reap_outcome',None);self._reference_reap_outcome=None
        if outcome=='success':self._reference_cli_force=False;self._reference_manual_id=None
        if active:
            self._reference_retry=None if outcome=='success' or getattr(self,'quit',False) else active['entry']
        self.reference_release_pin();self._reference_active=None
        try:self.reference_refresh(force=True)
        except (OSError,ValueError,RecursionError) as error:self.reference_error='参考图进度暂未更新：'+str(error)

    def reference_close(self):
        worker=getattr(self,'worker',None)
        if worker is None or worker.poll() is not None:
            self.reference_release_pin();self._reference_active=None
        access=getattr(self,'reference_access',None)
        close=getattr(access,'close',None)
        if callable(close):
            try:close()
            except Exception as error:self.reference_access_error=str(error)
