"""Persistent, task-first sampling, enabled only by an explicit device policy."""
import json
from pathlib import Path
import secrets
from reference_catalog import _durable, _encoded, unique_json, valid_id

def policy(base):
    path=Path(base)/'configs/task_block_policy.json'
    if not path.exists():return None
    value=unique_json(path.read_bytes())
    if (set(value)!={'version','list_ids','block_size'} or value['version']!=1
            or value['block_size'] not in (5,'all') or not isinstance(value['list_ids'],list)
            or not value['list_ids'] or len(set(value['list_ids']))!=len(value['list_ids'])
            or any(not valid_id(v) for v in value['list_ids'])):
        raise ValueError('任务分组抽取设置无效')
    return value

class TaskBlocks:
    def __init__(self,base,settings):
        self.round_robin=settings.get('round_robin') is True
        if self.round_robin:
            namespace=settings.get('namespace')
            if not isinstance(namespace,str) or not namespace.startswith('temporary-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in namespace):raise ValueError('临时任务命名空间无效')
            self.path=Path(base)/'runtime/text_task_blocks'/('blocks-'+namespace+'.json')
        else:self.path=Path(base)/'runtime/reference_task_blocks.json'
        self.ids=settings['list_ids'];self.size=settings['block_size']
        self.value=dict(version=1,current_task=None,target_size=None,completed_ids=[],finished_tasks=[],pending=None)
        if self.path.exists():
            value=unique_json(self.path.read_bytes())
            if (set(value)!=set(self.value) or value['version']!=1
                    or value['current_task'] not in [None]+self.ids
                    or (value['target_size'] is not None and (type(value['target_size']) is not int or not 1<=value['target_size']<=5000))
                    or not isinstance(value['completed_ids'],list) or len(value['completed_ids'])>(value['target_size'] or 0)
                    or len(set(value['completed_ids']))!=len(value['completed_ids'])
                    or any(not valid_id(i) for i in value['completed_ids'])
                    or not isinstance(value['finished_tasks'],list)
                    or any(i not in self.ids for i in value['finished_tasks'])
                    or (value['pending'] is not None and not valid_id(value['pending']))):
                raise ValueError('任务分组进度无效，请恢复备份')
            self.value=value

    def save(self):
        if self.round_robin:
            parent=self.path.parent
            if parent.is_symlink():raise ValueError('临时任务状态目录无效')
            parent.mkdir(mode=0o700,exist_ok=True)
        _durable(self.path,_encoded(self.value))

    def task(self,entry):
        item=entry['snapshot']['items'][entry['index']]
        return next((row['id'] for row in item.get('source_lists',[]) if row['id'] in self.ids),None)

    @staticmethod
    def image(entry):return entry['snapshot']['items'][entry['index']]['id']

    def success(self,entry):
        task=self.task(entry);identifier=self.image(entry)
        if task!=self.value['current_task']:raise ValueError('采集结果与当前分组任务不符')
        if identifier not in self.value['completed_ids']:self.value['completed_ids'].append(identifier)
        self.value['pending']=None
        if len(self.value['completed_ids'])==self.value['target_size']:
            if not self.round_robin:self.value['finished_tasks']=list(dict.fromkeys(self.value['finished_tasks']+[task]))
            self.value.update(current_task=None,target_size=None,completed_ids=[])
        self.save()

    def reconcile(self,snapshot,completed_paths):
        self._known_snapshot=snapshot;self._known_completed=tuple(completed_paths)
        # A host QC ERROR releases a provisional completion for re-capture.
        completed=set(completed_paths)
        valid={item['id'] for item in snapshot.get('items',[]) if item['relative_path'] in completed}
        changed=False
        if self.value['current_task'] is not None:
            kept=[identifier for identifier in self.value['completed_ids'] if identifier in valid]
            if kept!=self.value['completed_ids']:self.value['completed_ids']=kept;changed=True
        finished=[]
        for task in self.value['finished_tasks']:
            entries=[dict(snapshot=snapshot,index=i) for i in range(len(snapshot['items']))]
            task_ids={self.image(entry) for entry in entries if self.task(entry)==task}
            if not task_ids or task_ids<=valid:finished.append(task)
        if finished!=self.value['finished_tasks']:self.value['finished_tasks']=finished;changed=True
        if changed:self.save()
        pending=self.value['pending']
        item=next((i for i in snapshot.get('items',[]) if i['id']==pending),None)
        if item and item['relative_path'] in set(completed_paths):
            self.success(dict(snapshot=snapshot,index=snapshot['items'].index(item)))

    def choose(self,entries,choose_image):
        groups={}
        for entry in entries:
            task=self.task(entry)
            if task:groups.setdefault(task,[]).append(entry)
        current=self.value['current_task']
        if self.size=='all' and current is not None and not groups.get(current):
            self.value.update(current_task=None,target_size=None,completed_ids=[],pending=None);current=None
        if current is None and self.round_robin:
            counts=self.round_counts()
            available=[task for task in self.ids if groups.get(task) and counts.get(task,0)<50]
            if not available:raise ValueError('本轮文字任务已完成，没有下一项任务')
            lowest=min(counts.get(task,0)//self.size for task in available)
            current=secrets.choice([task for task in self.ids if task in available and counts.get(task,0)//self.size==lowest])
            self.value.update(current_task=current,target_size=min(self.size-(counts.get(current,0)%self.size),50-counts.get(current,0)),completed_ids=[],pending=None)
        if current is None:
            minimum=1 if self.size=='all' else self.size
            candidates=[task for task,rows in groups.items() if len(rows)>=minimum and task not in self.value['finished_tasks']]
            if not candidates:raise ValueError('本轮任务已完成，没有下一项任务')
            current=secrets.choice(candidates)
            target=len(groups[current]) if self.size=='all' else self.size
            if self.size=='all' and hasattr(self,'_known_snapshot'):
                self.switch_task(current,self._known_snapshot,self._known_completed)
            else:self.value.update(current_task=current,target_size=target,completed_ids=[],pending=None)
        rows=[e for e in groups.get(current,[]) if self.image(e) not in self.value['completed_ids']]
        if self.round_robin and self.value['pending'] is not None:
            chosen=next((entry for entry in rows if self.image(entry)==self.value['pending']),None)
            if chosen is None:raise ValueError('本次任务正在等待质检或恢复，请先处理结果')
        else:
            if not rows:raise ValueError('当前任务没有剩余图片，请检查任务库')
            chosen=choose_image(rows)
        self.value['pending']=self.image(chosen);self.save()
        return chosen

    def _switch_task_all(self,identifier,snapshot,completed_paths):
        """Explicit task change; completion truth stays in the durable ledger."""
        if self.size!='all':raise ValueError('当前抽取模式不支持手动更换任务列表')
        if identifier not in self.ids:raise ValueError('此列表不在本机场景任务范围内')
        entries=[dict(snapshot=snapshot,index=i) for i in range(len(snapshot['items']))]
        entries=[e for e in entries if self.task(e)==identifier]
        if not entries:raise ValueError('此列表未启用或没有图片，请先勾选该列表参与抽取')
        completed=set(completed_paths)
        done=[self.image(e) for e in entries if snapshot['items'][e['index']]['relative_path'] in completed]
        if len(done)==len(entries):raise ValueError('这个列表已完成，请选择其他未完成列表')
        if self.value['current_task']==identifier:return False
        previous=self.value
        candidate=dict(previous,current_task=identifier,target_size=len(entries),completed_ids=done,pending=None,
                       finished_tasks=[v for v in previous['finished_tasks'] if v!=identifier])
        self.value=candidate
        try:self.save()
        except Exception as error:
            if not getattr(error,'committed',False):self.value=previous
            raise
        return True

    def switch_task(self,identifier,snapshot,completed_paths):
        if self.size=='all':return self._switch_task_all(identifier,snapshot,completed_paths)
        if identifier not in self.ids:raise ValueError('任务不在本机场景中')
        rows=[item for item in snapshot['items'] if any(x['id']==identifier for x in item.get('source_lists',[]))]
        remaining=[item for item in rows if item['relative_path'] not in set(completed_paths)]
        if not rows:raise ValueError('这个任务列表未启用')
        if self.value['current_task']==identifier:return False
        if not remaining:raise ValueError('这个任务列表已完成')
        target=len(remaining) if self.size=='all' else min(self.size,len(remaining))
        self.value.update(current_task=identifier,target_size=target,completed_ids=[],pending=None,
            finished_tasks=[x for x in self.value['finished_tasks'] if x!=identifier])
        self.save();return True

    def round_counts(self):
        counts={task:0 for task in self.ids}
        completed=set(getattr(self,'_known_completed',()))
        for i,item in enumerate(getattr(self,'_known_snapshot',{}).get('items',[])):
            if item['relative_path'] in completed:
                task=self.task(dict(snapshot=self._known_snapshot,index=i))
                if task in counts:counts[task]+=1
        return counts

    def status(self):
        result=dict(selection_mode='task_until_exhausted' if self.size=='all' else 'task_blocks_random_without_replacement',block_size=self.value['target_size'] or self.size,
                    block_completed=len(self.value['completed_ids']),block_task_id=self.value['current_task'],
                    manual_task_switch=not self.round_robin,allowed_task_ids=list(self.ids))
        if self.round_robin:result.update(round_robin=True,task_completed=self.round_counts().get(self.value['current_task'],0),task_target=50)
        return result
