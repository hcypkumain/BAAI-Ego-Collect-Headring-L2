"""Manual task/image selection before START, preserving a prepared EEG owner."""
import copy,hashlib,json,os,secrets,shutil,time,uuid
from pathlib import Path
from reference_catalog import _durable,_encoded,valid_id

def can_select(owner,_transaction=None):
    if getattr(owner,'_reference_transaction',None) is not None and getattr(owner,'_reference_transaction',None) is not _transaction:return False
    if owner.reference_can_change():return True
    if getattr(owner,'state',None)!='ready' or getattr(owner,'quit',False):return False
    path=getattr(owner,'path',None);worker=getattr(owner,'worker',None)
    if not path or worker is None or worker.poll() is not None or (path/'start_gate.json').exists():return False
    status=owner.worker_status();privacy=owner.dashboard.privacy.snapshot()
    if status.get('state')!='ready' or status.get('recording_active'):return False
    if privacy.get('required') is not False or privacy.get('stop_confirmed') is not True or privacy.get('phase')!='idle':return False
    if 'eeg' in (getattr(owner,'selected_devices',None) or ()):
        from dashboard import impedance_allows_recording
        if not impedance_allows_recording(status.get('eeg_impedance')):return False
    return True

def _bundle_evidence(path):
    if not path.exists():return None
    if path.is_symlink() or not path.is_dir():raise ValueError('参考资料目录无效')
    result={}
    def onerror(error):raise error
    for root,dirs,files in os.walk(path,followlinks=False,onerror=onerror):
        for name in dirs+files:
            item=Path(root)/name
            if item.is_symlink():raise ValueError('参考资料目录包含链接')
            relative=item.relative_to(path).as_posix()
            result[relative]=None if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest()
    return result

def recover_prepared_transaction(owner):
    """Explicitly clear a latch only after the old state is fully restored."""
    owner._assert_controller_thread()
    transaction=getattr(owner,'_reference_transaction',None)
    if transaction is None:return False
    path=transaction['path'];blocks=owner._reference_task_blocks
    try:
        if ((path/'meta.json').read_bytes()!=transaction['old_meta'] or (path/'reference_queue.json').read_bytes()!=transaction['old_queue']
                or _bundle_evidence(path/'reference_bundle')!=transaction['old_bundle']
                or owner._reference_active is not transaction['old_active'] or owner.image!=transaction['old_image']
                or owner.reference_catalog.pins!=transaction['old_pins'] or blocks.value!=transaction['old_progress']
                or (transaction['old_progress_bytes'] is not None and json.loads(blocks.path.read_bytes())!=json.loads(transaction['old_progress_bytes']))):
            raise ValueError('参考图回滚校验不一致')
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise ValueError('参考图恢复未确认，请保留原始证据') from error
    owner._reference_mutation_started()
    owner._reference_transaction=None
    return True

def prepared_start_error(owner):
    if getattr(owner,'_reference_transaction',None) is not None:
        return '参考图切换恢复未确认，请保留本段并联系维护人员'
    active=getattr(owner,'_reference_active',None)
    if not active:return None
    try:
        path=Path(owner.path);entry=active['entry'];item=entry['snapshot']['items'][entry['index']]
        if (active['path']!=path or owner.image!=entry['path']
                or json.loads((path/'meta.json').read_bytes())!=active['raw_meta']
                or json.loads((path/'reference_queue.json').read_bytes())!=entry['snapshot']):
            raise ValueError('参考图元数据不一致')
        blocks=getattr(owner,'_reference_task_blocks',None)
        if blocks and blocks.value['pending']!=item['id']:raise ValueError('参考图进度不一致')
        if active['owner'] not in owner.reference_catalog.pins.get(item['id'],set()):raise ValueError('参考图锁不一致')
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        owner._reference_transaction=dict(path=getattr(owner,'path',None),primary_error=repr(error),rollback_errors=[])
        return '参考图准备证据不一致，请保留本段并联系维护人员'
    return None

def _prepared_commit(owner,entry,transaction):
    path=Path(owner.path);old=owner._reference_active
    if not old or old.get('path')!=path:raise ValueError('当前准备记录未同步，请重新准备')
    catalog=owner.reference_catalog;snapshot=entry['snapshot'];item=snapshot['items'][entry['index']]
    token=transaction['token'];history=transaction['history']
    old_meta=transaction['old_meta'];old_queue=transaction['old_queue']
    bundle=path/'reference_bundle';moved=False;bundle_changed=False;committed=False
    try:
        history.mkdir(parents=True)
        (history/'meta.before.json').write_bytes(old_meta);(history/'queue.before.json').write_bytes(old_queue)
        catalog.pin(item['id'],token)
        selection=catalog.per_item_selection(snapshot,entry['index'],owner_token=token)
        scene=catalog.snapshot_scene(snapshot,history,entry['index'])
        transaction['new_bundle']=_bundle_evidence(history/'reference_bundle') if scene else None
        metadata=json.loads(old_meta.decode('utf-8'))
        metadata.update(reference_image=selection['asset']['relative_path'],reference_sha256=selection['asset']['sha256'],reference_selection=selection)
        metadata.pop('reference_bundle',None);metadata.pop('reference_layout_status',None)
        if scene:metadata.update(reference_bundle=scene,reference_layout_status='bundle_snapshot')
        if not can_select(owner,transaction):raise ValueError('采集状态已变化，请重新选择')
        if bundle.exists():os.replace(bundle,history/'bundle.before');moved=True;bundle_changed=True
        if scene:os.replace(history/'reference_bundle',bundle);bundle_changed=True
        _durable(path/'reference_queue.json',_encoded(snapshot))
        _durable(path/'meta.json',_encoded(metadata))
        active=dict(owner=token,entry=entry,path=path,identity=copy.deepcopy(old.get('identity')),
                    selection=selection,raw_meta=copy.deepcopy(metadata))
        if scene:active['scene_snapshot']=scene
        owner._reference_active=active;owner.image=entry['path'];committed=True
        transaction['committed']=True
        try:catalog.unpin(old['owner'])
        except Exception:pass
        try:_durable(history/'result.json',_encoded(dict(committed=True,image_id=item['id'],worker_preserved=True,at=time.time())))
        except Exception as error:transaction['audit_error']=repr(error)
    except BaseException as error:
        transaction['primary_error']=repr(error)+((' caused by '+repr(error.__cause__)) if error.__cause__ else '')
        if not committed:
            operations=[lambda:_durable(path/'meta.json',old_meta),lambda:_durable(path/'reference_queue.json',old_queue)]
            if bundle_changed and bundle.exists():operations.append(lambda:os.replace(bundle,history/'bundle.failed'))
            if moved:operations.append(lambda:os.replace(history/'bundle.before',bundle))
            operations.append(lambda:catalog.unpin(token))
            for operation in operations:
                try:operation()
                except Exception as rollback_error:transaction['rollback_errors'].append(repr(rollback_error))
        raise

def choose(owner,value=None,next_image=False,list_only=False):
    owner._assert_controller_thread()
    if not can_select(owner):raise ValueError('请先完成阻抗检查；正式采集开始后不能更换任务或参考图')
    blocks=owner._reference_task_blocks
    if list_only:
        if not isinstance(value,dict) or set(value)!={'list_id','catalog_revision'}:raise ValueError('任务列表选择无效')
    elif not next_image:
        if not isinstance(value,dict) or set(value) not in ({'image_id','catalog_revision'},{'image_id','catalog_revision','list_id'}):raise ValueError('参考图选择无效')
    revision=owner.reference_catalog.revision if next_image else value['catalog_revision']
    if type(revision) is not int:raise ValueError('图库版本无效')
    snapshot=owner.reference_catalog.freeze_list('pool',revision,include_completed=True)
    preferred=(value or {}).get('list_id')
    if preferred is not None and (not valid_id(preferred) or preferred not in blocks.ids):raise ValueError('请选择本机采集范围内的具体任务列表')
    entries=[dict(snapshot=snapshot,index=i,path=owner._reference_project/item['relative_path']) for i,item in enumerate(snapshot['items'])]
    completed=tuple(owner.reference_completed_paths());completed_set=set(completed)
    if list_only or next_image:
        task=preferred or blocks.value['current_task']
        reserved=set(getattr(owner,'_reference_reserved',()))
        waiting=set(blocks.awaiting_qc())
        candidates=[e for e in entries if blocks.task(e)==task and str(e['path'].relative_to(owner._reference_project)) not in completed_set
                    and str(e['path'].relative_to(owner._reference_project)) not in reserved and blocks.image(e) not in waiting]
        previous=getattr(owner,'image',None)
        if next_image:candidates=[e for e in candidates if e['path']!=previous] or candidates
        if not candidates:raise ValueError('此任务没有待采集图片，请选择其他列表')
        entry=owner._reference_image_choice(candidates)
    else:
        identifier=value['image_id']
        entry=next((e for e in entries if snapshot['items'][e['index']]['id']==identifier and (preferred is None or blocks.task(e)==preferred)),None)
        if entry is None:raise ValueError('所选图片不在本机启用的任务列表中')
        if str(entry['path'].relative_to(owner._reference_project)) in completed_set:raise ValueError('这张参考图已完成，请选择尚未完成的参考图')
        if str(entry['path'].relative_to(owner._reference_project)) in set(getattr(owner,'_reference_reserved',())):
            raise ValueError('该参考图等待完整质检，暂不能重复抽取')
    task=blocks.task(entry);identifier=blocks.image(entry)
    if task is None:raise ValueError('所选参考图没有可用任务列表')
    if task!=blocks.value['current_task'] and blocks.awaiting_qc() and getattr(owner,'worker',None):
        raise ValueError('当前任务的质检状态需要刷新，请先退出准备后重试')
    owner._reference_mutation_started()
    previous=copy.deepcopy(blocks.value)
    transaction=None
    if getattr(owner,'worker',None):
        path=Path(owner.path);token=uuid.uuid4().hex
        parent=next((p.parent for p in path.parents if p.name=='data'),owner._reference_base)
        transaction=dict(path=path,token=token,history=parent/'diagnostics'/'reference-switches'/path.name/token,
            old_id=blocks.value.get('pending'),new_id=identifier,rollback_errors=[],committed=False)
        owner._reference_transaction=transaction
        try:
            transaction.update(old_meta=(path/'meta.json').read_bytes(),old_queue=(path/'reference_queue.json').read_bytes(),
                old_bundle=_bundle_evidence(path/'reference_bundle'),old_active=owner._reference_active,
                old_image=owner.image,old_pins=copy.deepcopy(owner.reference_catalog.pins),old_progress=copy.deepcopy(previous),
                old_progress_bytes=blocks.path.read_bytes() if blocks.path.exists() else None)
        except BaseException as error:
            transaction['primary_error']=repr(error);raise
    try:
        blocks.switch_task(task,snapshot,completed)
        blocks.reserve(entry)
        if transaction:
            _prepared_commit(owner,entry,transaction)
            active=owner._reference_active
            if (json.loads((path/'meta.json').read_bytes())!=active['raw_meta']
                    or json.loads((path/'reference_queue.json').read_bytes())!=snapshot
                    or blocks.value['pending']!=identifier or json.loads(blocks.path.read_bytes())!=blocks.value or owner.image!=entry['path']
                    or _bundle_evidence(path/'reference_bundle')!=transaction['new_bundle']
                    or token not in owner.reference_catalog.pins.get(identifier,set())
                    or any(transaction['old_active']['owner'] in owners for owners in owner.reference_catalog.pins.values())):
                raise ValueError('参考图切换提交后校验失败，需人工恢复')
            owner._reference_transaction=None
        else:owner._reference_active=None;owner.image=entry['path']
    except BaseException as error:
        if transaction is not None:
            transaction.setdefault('primary_error',repr(error))
            if transaction['committed']:raise
        try:
            blocks.value=previous;blocks.save()
        except Exception as rollback_error:
            if transaction is not None:transaction['rollback_errors'].append(repr(rollback_error))
            raise
        if transaction is not None and not transaction['rollback_errors']:
            try:
                recover_prepared_transaction(owner)
            except Exception as verify_error:transaction['rollback_errors'].append(repr(verify_error))
        raise
    owner._reference_retry=None;owner._reference_cli_image=None;owner._reference_cli_force=False
    owner._reference_manual_id=identifier;owner._reference_completed=completed
    owner._reference_snapshot=snapshot
    owner._apply_reference_snapshot(snapshot);owner._update_reference_task()
    owner.reference_error=None;owner.selected_image=None
    owner.state_write()
    if getattr(owner,'worker',None) is None and owner.state!='device_confirm' and owner.selected_devices:
        owner.state='idle';owner.prepare()
