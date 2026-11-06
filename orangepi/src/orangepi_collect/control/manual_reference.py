"""Manual task/image selection before START, preserving a prepared EEG owner."""
import copy,json,os,secrets,shutil,time,uuid
from pathlib import Path
from reference_catalog import _durable,_encoded,valid_id

def can_select(owner):
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

def _prepared_commit(owner,entry):
    if not can_select(owner):raise ValueError('请完成阻抗检查，并保持尚未正式开录')
    path=Path(owner.path);old=owner._reference_active
    if not old or old.get('path')!=path:raise ValueError('当前准备记录未同步，请重新准备')
    catalog=owner.reference_catalog;snapshot=entry['snapshot'];item=snapshot['items'][entry['index']]
    token=uuid.uuid4().hex;catalog.pin(item['id'],token)
    parent=next((p.parent for p in path.parents if p.name=='data'),owner._reference_base)
    history=parent/'diagnostics'/'reference-switches'/path.name/token;history.mkdir(parents=True)
    old_meta=(path/'meta.json').read_bytes();old_queue=(path/'reference_queue.json').read_bytes()
    (history/'meta.before.json').write_bytes(old_meta);(history/'queue.before.json').write_bytes(old_queue)
    bundle=path/'reference_bundle';moved=False;bundle_changed=False;committed=False
    try:
        selection=catalog.per_item_selection(snapshot,entry['index'],owner_token=token)
        scene=catalog.snapshot_scene(snapshot,history,entry['index'])
        metadata=json.loads(old_meta.decode('utf-8'))
        metadata.update(reference_image=selection['asset']['relative_path'],reference_sha256=selection['asset']['sha256'],reference_selection=selection)
        metadata.pop('reference_bundle',None);metadata.pop('reference_layout_status',None)
        if scene:metadata.update(reference_bundle=scene,reference_layout_status='bundle_snapshot')
        if not can_select(owner):raise ValueError('采集状态已变化，请重新选择')
        if bundle.exists():os.replace(bundle,history/'bundle.before');moved=True;bundle_changed=True
        if scene:os.replace(history/'reference_bundle',bundle);bundle_changed=True
        _durable(path/'reference_queue.json',_encoded(snapshot))
        _durable(path/'meta.json',_encoded(metadata))
        active=dict(owner=token,entry=entry,path=path,identity=copy.deepcopy(old.get('identity')),
                    selection=selection,raw_meta=copy.deepcopy(metadata))
        if scene:active['scene_snapshot']=scene
        owner._reference_active=active;owner.image=entry['path'];committed=True
        try:catalog.unpin(old['owner'])
        except Exception:pass
        try:_durable(history/'result.json',_encoded(dict(committed=True,image_id=item['id'],worker_preserved=True,at=time.time())))
        except OSError:pass
    except BaseException:
        if not committed:
            _durable(path/'meta.json',old_meta);_durable(path/'reference_queue.json',old_queue)
            if bundle_changed and bundle.exists():os.replace(bundle,history/'bundle.failed')
            if moved:os.replace(history/'bundle.before',bundle)
            catalog.unpin(token)
        raise

def choose(owner,value=None,next_image=False,list_only=False):
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
        candidates=[e for e in entries if blocks.task(e)==task and str(e['path'].relative_to(owner._reference_project)) not in completed_set]
        previous=getattr(owner,'image',None)
        if next_image:candidates=[e for e in candidates if e['path']!=previous] or candidates
        if not candidates:raise ValueError('此任务没有待采集图片，请选择其他列表')
        entry=owner._reference_image_choice(candidates)
    else:
        identifier=value['image_id']
        entry=next((e for e in entries if snapshot['items'][e['index']]['id']==identifier and (preferred is None or blocks.task(e)==preferred)),None)
        if entry is None:raise ValueError('所选图片不在本机启用的任务列表中')
        if str(entry['path'].relative_to(owner._reference_project)) in completed_set:raise ValueError('这张参考图已完成，请选择尚未完成的参考图')
    task=blocks.task(entry);identifier=blocks.image(entry)
    if task is None:raise ValueError('所选参考图没有可用任务列表')
    previous=copy.deepcopy(blocks.value)
    try:
        blocks.switch_task(task,snapshot,completed)
        blocks.value['pending']=identifier;blocks.save()
        if getattr(owner,'worker',None):_prepared_commit(owner,entry)
        else:owner._reference_active=None;owner.image=entry['path']
    except BaseException:
        blocks.value=previous;blocks.save();raise
    owner._reference_retry=None;owner._reference_cli_image=None;owner._reference_cli_force=False
    owner._reference_manual_id=identifier;owner._reference_completed=completed
    owner._reference_snapshot=snapshot
    owner._apply_reference_snapshot(snapshot);owner._update_reference_task()
    owner.reference_error=None;owner.selected_image=None
    owner.state_write()
    if getattr(owner,'worker',None) is None and owner.state!='device_confirm' and owner.selected_devices:
        owner.state='idle';owner.prepare()
