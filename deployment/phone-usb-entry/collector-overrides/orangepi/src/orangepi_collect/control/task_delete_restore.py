"""Conservative, idempotent saved-task correction after a journaled deletion."""
import fcntl
import contextlib
from pathlib import Path
from reference_catalog import _durable,_encoded,unique_json

def restore_saved_reference(base,project,ledger,relative_path,deleted_session,already_locked=False):
    """Return restored/manual; refuse ambiguous ownership and legacy state."""
    base=Path(base);project=Path(project).resolve();image=(project/relative_path).resolve()
    try:image.relative_to((project/'configs/environments').resolve())
    except ValueError:return {'status':'manual','reason':'参考图路径无效'}
    runtime=base/'runtime';lockpath=runtime/'ledger.lock'
    with (contextlib.nullcontext() if already_locked else lockpath.open('a')) as lock:
        if not already_locked:fcntl.flock(lock,fcntl.LOCK_EX)
        data=ledger.read(image.parent/'used.yaml');used=data.get('used',{}).get(image.name);attempts=data.get('attempts',{}).get(image.name,[])
        if not isinstance(attempts,list) or any(not isinstance(x,dict) for x in attempts):return {'status':'manual','reason':'参考图历史记录不完整'}
        own=[x for x in attempts if x.get('session')==str(deleted_session) and x.get('deleted') is True and x.get('status') in ('success','pending_qc') and x.get('has_data') is True]
        if len(own)!=1:return {'status':'manual','reason':'缺少被删除段的唯一已保存证据'}
        if used is not None:return {'status':'manual','reason':'参考图仍由另一段占用'}
        for x in attempts:
            if x.get('session')!=str(deleted_session) and not x.get('deleted') and x.get('status') in ('success','pending_qc'):
                return {'status':'manual','reason':'参考图还有其他已保存记录'}
        paths=[runtime/'reference_task_blocks.json'];text_dir=runtime/'text_task_blocks'
        if text_dir.is_symlink():return {'status':'manual','reason':'任务状态目录无效'}
        if text_dir.is_dir():
            paths+=sorted(text_dir.glob('blocks-temporary-*.json'))
        if len(paths)>501:return {'status':'manual','reason':'任务状态文件过多'}
        changes=[]
        for p in paths:
            if not p.exists():continue
            if p.is_symlink() or not p.is_file() or p.stat().st_size>4_000_000:return {'status':'manual','reason':'任务状态文件无效'}
            v=unique_json(p.read_bytes())
            if not isinstance(v,dict) or v.get('version')!=4 or not isinstance(v.get('image_refs'),dict):return {'status':'manual','reason':'任务状态版本不支持自动恢复'}
            ids=[i for i,r in v['image_refs'].items() if isinstance(r,dict) and r.get('relative_path')==relative_path]
            if len(ids)>1:return {'status':'manual','reason':'同一参考图对应多个任务标识'}
            if not ids:continue
            ident=ids[0];ref=v['image_refs'][ident];task=ref.get('task');history=v.get('saved_history');selected=v.get('selected_ids');saved=v.get('saved_ids');completed=v.get('completed_ids');finished=v.get('finished_tasks');proofs=v.get('finished_images')
            if any(not isinstance(x,list) or any(not isinstance(i,str) for i in x) or len(x)!=len(set(x)) for x in (history,selected,saved,completed,finished)) or not isinstance(proofs,dict):return {'status':'manual','reason':'任务进度结构无效'}
            if v.get('pending')==ident:return {'status':'manual','reason':'参考图正在准备'}
            if not isinstance(task,str):return {'status':'manual','reason':'参考图任务标识无效'}
            if task in finished and task not in proofs:return {'status':'manual','reason':'历史任务缺少逐图完成证明'}
            if any(not isinstance(x,list) or any(not isinstance(i,str) for i in x) for x in proofs.values()):return {'status':'manual','reason':'历史任务证明无效'}
            if ident in history and v.get('current_task')==task and ident not in saved and type(v.get('block_saved_start')) is int:
                task_history=[i for i in history if isinstance(v['image_refs'].get(i),dict) and v['image_refs'][i].get('task')==task]
                if ident in task_history and task_history.index(ident)<v['block_saved_start']:v['block_saved_start']-=1
            for key in ('saved_history','saved_ids','completed_ids','selected_ids'):v[key]=[i for i in v[key] if i!=ident]
            for name,items in list(proofs.items()):
                if ident in items:proofs.pop(name);v['finished_tasks']=[i for i in v['finished_tasks'] if i!=name]
            changes.append((p,v))
        if not changes:return {'status':'manual','reason':'未找到对应参考图任务状态，需人工核对'}
        for p,v in changes:_durable(p,_encoded(v))
        return {'status':'restored','reason':'参考图和可证明的任务计数已恢复'}
