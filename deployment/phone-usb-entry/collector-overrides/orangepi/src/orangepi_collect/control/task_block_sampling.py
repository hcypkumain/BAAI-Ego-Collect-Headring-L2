"""Persistent, task-first sampling, enabled only by an explicit device policy."""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import tempfile
from reference_catalog import _durable, _encoded, unique_json, valid_id

def _ids(value):
    return isinstance(value,list) and all(valid_id(i) for i in value) and len(set(value))==len(value)

def policy(base):
    path=Path(base)/'configs/task_block_policy.json'
    if not path.exists():return None
    value=unique_json(path.read_bytes())
    if (not isinstance(value,dict) or set(value) not in ({'version','list_ids','block_size'},{'version','list_ids','block_size','count_basis'})
            or type(value['version']) is not int or value['version']!=1
            or not (value['block_size']=='all' or (type(value['block_size']) is int and 1<=value['block_size']<=5000))
            or value.get('count_basis','saved') not in ('qualified','saved')
            or not _ids(value['list_ids']) or not value['list_ids']):
        raise ValueError('任务分组抽取设置无效')
    return value

def ensure_default_policy(base,catalog):
    """Initialize this profile from its own enabled image lists, never another Pi."""
    existing=policy(base)
    if existing is not None:return existing
    images=catalog.get('images',{});lists=catalog.get('lists',{})
    ids=[]
    for identifier in catalog.get('list_order',list(lists)):
        row=lists.get(identifier,{})
        if (row.get('deleted') or row.get('hidden') or not row.get('sampling_enabled',True)
                or row.get('text_task_namespace') or row.get('text_task_list_ids')):continue
        if any(not images.get(i,{}).get('deleted',False) and images.get(i,{}).get('kind','image')=='image'
               for i in row.get('items',[]) if i in images):ids.append(identifier)
    if not ids:return None
    value=dict(version=1,list_ids=ids,block_size=5,count_basis='saved')
    parent=Path(base)/'configs'
    if parent.is_symlink():raise ValueError('任务分组配置目录无效')
    parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.task-block-default-',dir=str(parent))
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(_encoded(value));stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,str(parent/'task_block_policy.json'))
        except FileExistsError:return policy(base)
        directory=os.open(str(parent),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:os.unlink(temporary)
    return value

class TaskBlocks:
    def __init__(self,base,settings):
        self.round_robin=settings.get('round_robin') is True
        if self.round_robin:
            namespace=settings.get('namespace')
            if (not isinstance(namespace,str) or not namespace.startswith('temporary-')
                    or not 1<=len(namespace[10:])<=80
                    or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in namespace)):
                raise ValueError('临时任务命名空间无效')
            self.path=Path(base)/'runtime/text_task_blocks'/('blocks-'+namespace+'.json')
        else:self.path=Path(base)/'runtime/reference_task_blocks.json'
        self.ids=settings['list_ids'];self.size=settings['block_size']
        self.count_basis=settings.get('count_basis','saved')
        if self.count_basis not in ('qualified','saved'):raise ValueError('任务轮换计数口径无效')
        self.value=dict(version=4,current_task=None,target_size=None,completed_ids=[],finished_tasks=[],pending=None,
                        selected_ids=[],finished_images={},image_refs={},saved_ids=[],saved_history=[],block_saved_start=None)
        if self.path.exists():
            value=unique_json(self.path.read_bytes())
            if isinstance(value,dict) and 'block_saved_start' not in value:value=dict(value,block_saved_start=None)
            legacy=set(self.value)-{'selected_ids','finished_images','image_refs','saved_ids','saved_history'}
            if isinstance(value,dict) and set(value)==legacy and type(value['version']) is int and value['version']==1:
                if not _ids(value['completed_ids']) or (value['pending'] is not None and not valid_id(value['pending'])):
                    raise ValueError('任务分组进度无效，请恢复备份')
                # Preserve known progress only; old overwritten selections are unknown.
                value=dict(value,version=2,selected_ids=list(dict.fromkeys(value['completed_ids']+([value['pending']] if value['pending'] else []))),finished_images={},image_refs={})
            elif isinstance(value,dict) and set(value)==set(self.value)-{'image_refs','saved_ids','saved_history'}:
                value=dict(value,image_refs={})
            if (isinstance(value,dict) and set(value)==set(self.value)-{'saved_ids','saved_history'}
                    and type(value.get('version')) is int and value['version']==2):
                # V2 already durably distinguishes an unsaved pending draft from
                # selected non-pending recordings acknowledged by success/defer.
                # Preserve those save acknowledgements, never invent QC passes.
                value=dict(value,version=3,saved_ids=[i for i in value['selected_ids'] if i!=value['pending']])
            if (isinstance(value,dict) and set(value)==set(self.value)-{'saved_history'}
                    and type(value.get('version')) is int and value['version']==3):
                known=value['saved_ids']+[i for items in value['finished_images'].values() for i in items]
                value=dict(value,version=4,saved_history=list(dict.fromkeys(i for i in known if i in value['image_refs'])))
            if (not isinstance(value,dict) or set(value)!=set(self.value) or type(value['version']) is not int or value['version']!=4
                    or value['current_task'] not in [None]+self.ids
                    or (value['target_size'] is not None and (type(value['target_size']) is not int or not 1<=value['target_size']<=5000))
                    or not _ids(value['completed_ids']) or len(value['completed_ids'])>(value['target_size'] or 0)
                    or not _ids(value['finished_tasks'])
                    or any(i not in self.ids for i in value['finished_tasks'])
                    or (value['pending'] is not None and not valid_id(value['pending']))
                    or not _ids(value['selected_ids'])
                    or not _ids(value['saved_ids']) or not set(value['saved_ids'])<=set(value['selected_ids'])
                    or not isinstance(value['image_refs'],dict)
                    or not _ids(value['saved_history']) or not set(value['saved_history'])<=set(value['image_refs'])
                    or (value['block_saved_start'] is not None and (type(value['block_saved_start']) is not int or not 0<=value['block_saved_start']<=5000))
                    or value['pending'] in value['saved_ids']
                    or not set(value['completed_ids'])<=set(value['selected_ids'])
                    or ((value['current_task'] is None)!=(value['target_size'] is None))
                    or (value['current_task'] is None and (value['selected_ids'] or value['completed_ids'] or value['saved_ids'] or value['pending'] is not None or value['block_saved_start'] is not None))
                    or (value['target_size'] is not None and len(value['selected_ids'])>value['target_size'])
                    or (value['pending'] is not None and (value['pending'] not in value['selected_ids'] or value['pending'] in value['completed_ids']))
                    or value['current_task'] in value['finished_tasks']
                    or not isinstance(value['finished_images'],dict)
                    or any(task not in value['finished_tasks'] or not _ids(images) or not images
                           for task,images in value['finished_images'].items())
                    or not self._valid_refs(value)):
                raise ValueError('任务分组进度无效，请恢复备份')
            self.value=value
        self._saved=copy.deepcopy(self.value)

    def _valid_refs(self,value):
        refs=value.get('image_refs')
        if not isinstance(refs,dict):return False
        for identifier,ref in refs.items():
            if not valid_id(identifier) or not isinstance(ref,dict) or set(ref)!={'task','relative_path'}:return False
            path=ref['relative_path']
            if ref['task'] not in self.ids or not isinstance(path,str) or not path or '\\' in path:return False
            parts=PurePosixPath(path)
            if parts.is_absolute() or any(p in ('.','..') for p in path.split('/')):return False
            if identifier in value['selected_ids'] and ref['task']!=value['current_task']:return False
            if any(identifier in images and ref['task']!=task for task,images in value['finished_images'].items()):return False
        return True

    def _remember(self,entry):
        item=entry['snapshot']['items'][entry['index']];identifier=item['id']
        ref=dict(task=self.task(entry),relative_path=item['relative_path'])
        previous=self.value['image_refs'].get(identifier)
        if previous is not None and previous!=ref:raise ValueError('参考图关联已变化，请恢复原始证据')
        self.value['image_refs'][identifier]=ref

    def unresolved(self):
        refs=self.value['image_refs']
        known=self.value['selected_ids']+[i for images in self.value['finished_images'].values() for i in images]
        return tuple(dict.fromkeys(i for i in known if i not in refs))

    def save(self):
        if self.round_robin:
            if self.path.parent.is_symlink():raise ValueError('临时任务状态目录无效')
            self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        candidate=copy.deepcopy(self.value)
        candidate['selected_ids']=list(dict.fromkeys(candidate['selected_ids']+candidate['completed_ids']+
            ([candidate['pending']] if candidate['pending'] else [])))
        self.value=candidate
        try:_durable(self.path,_encoded(candidate))
        except BaseException as error:
            if not getattr(error,'committed',False):self.value=copy.deepcopy(self._saved)
            else:self._saved=copy.deepcopy(candidate)
            raise
        self._saved=copy.deepcopy(candidate)

    def task(self,entry):
        item=entry['snapshot']['items'][entry['index']]
        return next((row['id'] for row in item.get('source_lists',[]) if row['id'] in self.ids),None)

    @staticmethod
    def image(entry):return entry['snapshot']['items'][entry['index']]['id']

    def awaiting_qc(self):
        return tuple(i for i in self.value['selected_ids']
                     if i not in self.value['completed_ids'] and i!=self.value['pending'])

    def reserve(self,entry):
        identifier=self.image(entry)
        if self.task(entry)!=self.value['current_task']:raise ValueError('参考图不属于当前任务分组')
        if identifier in self.value['completed_ids']:raise ValueError('该参考图已完成')
        if self.count_basis=='saved' and identifier in self.value['saved_ids']:raise ValueError('该参考图本块已保存')
        if identifier in self.awaiting_qc():raise ValueError('该参考图正在等待质检')
        previous=self.value['pending']
        selected=[i for i in self.value['selected_ids'] if i!=previous or i in self.value['completed_ids']]
        if identifier not in selected and len(selected)>=self.value['target_size']:
            raise ValueError('当前任务块正在等待质检，请在结果返回后继续')
        self._remember(entry)
        if self.round_robin and self.count_basis=='saved' and identifier in self.value['saved_history']:
            raise ValueError('本轮文字任务的该次采集已保存')
        self.value.update(pending=identifier,selected_ids=list(dict.fromkeys(selected+[identifier])))
        self.save()

    def defer(self,entry):
        """A saved recording waits for QC; it is not an unsaved preparation."""
        identifier=self.image(entry)
        if self.task(entry)!=self.value['current_task']:raise ValueError('待质检记录与当前任务分组不符')
        if identifier not in self.value['selected_ids']:raise ValueError('待质检记录缺少已保留的参考图关联')
        self._remember(entry)
        if identifier not in self.value['saved_history']:self.value['saved_history'].append(identifier)
        self.value['selected_ids']=list(dict.fromkeys(self.value['selected_ids']+[identifier]))
        if self.value['pending']==identifier:self.value['pending']=None
        if identifier not in self.value['saved_ids']:self.value['saved_ids'].append(identifier)
        self._finish_saved_block()
        self.save()

    def _finish_block(self,identifiers):
        task=self.value['current_task']
        if not self.round_robin:
            self.value['finished_tasks']=list(dict.fromkeys(self.value['finished_tasks']+[task]))
            self.value['finished_images'][task]=list(identifiers)
        self.value.update(current_task=None,target_size=None,completed_ids=[],pending=None,selected_ids=[],saved_ids=[],block_saved_start=None)

    def _finish_saved_block(self):
        if (self.count_basis=='saved' and self.value['current_task'] is not None
                and len(self.value['saved_ids'])==self.value['target_size']):
            self._finish_block(self.value['saved_ids'])
            return True
        return False

    def cancel_pending(self):
        identifier=self.value['pending']
        if identifier is None:return False
        self.value['pending']=None
        if identifier not in self.value['completed_ids']:
            self.value['selected_ids']=[i for i in self.value['selected_ids'] if i!=identifier]
        self.save();return True

    def success(self,entry):
        task=self.task(entry);identifier=self.image(entry)
        if task!=self.value['current_task']:raise ValueError('采集结果与当前分组任务不符')
        if identifier not in self.value['selected_ids']:raise ValueError('采集结果缺少已保留的参考图关联')
        self._remember(entry)
        if identifier not in self.value['saved_history']:self.value['saved_history'].append(identifier)
        if identifier not in self.value['completed_ids']:self.value['completed_ids'].append(identifier)
        if identifier not in self.value['saved_ids']:self.value['saved_ids'].append(identifier)
        if self.value['pending']==identifier:self.value['pending']=None
        if self.count_basis=='saved':self._finish_saved_block()
        elif len(self.value['completed_ids'])==self.value['target_size']:
            self._finish_block(self.value['completed_ids'])
        self.save()

    def reconcile(self,snapshot,completed_paths,reserved_paths=None):
        self._known_snapshot=snapshot;self._known_completed=tuple(completed_paths)
        self._known_reserved=tuple(reserved_paths or ())
        completed=set(completed_paths)
        items={item['id']:item for item in snapshot.get('items',[])}
        refs=self.value['image_refs']
        changed=False
        # Only a same-task visible item can supply missing legacy evidence.
        known_tasks={i:self.value['current_task'] for i in self.value['selected_ids']}
        for task,identifiers in self.value['finished_images'].items():
            for identifier in identifiers:known_tasks[identifier]=task
        for identifier,task in known_tasks.items():
            item=items.get(identifier)
            if identifier not in refs and item:
                entry=dict(snapshot=snapshot,index=snapshot['items'].index(item))
                if self.task(entry)==task:self._remember(entry);changed=True
        # A legacy text namespace has one immutable occurrence per take. Recover
        # only locally evidenced saved occurrences; future history is durable and
        # never depends on a later remote QC receipt.
        if self.round_robin and self.count_basis=='saved':
            saved_paths=completed|set(reserved_paths or ())
            for i,item in enumerate(snapshot.get('items',[])):
                if item['relative_path'] in saved_paths and item['id'] not in self.value['saved_history']:
                    entry=dict(snapshot=snapshot,index=i)
                    if self.task(entry) is not None:
                        self._remember(entry);self.value['saved_history'].append(item['id']);changed=True
            current=self.value['current_task']
            if current is not None and self.value['block_saved_start'] is None:
                # V1 text state retained only its last pending occurrence. Recover
                # this block from immutable, locally evidenced occurrences rather
                # than requiring a remote QC acknowledgement for each one.
                history=[i for i in self.value['saved_history'] if refs[i]['task']==current]
                count=len(history);offset=(self.size-self.value['target_size'])%self.size
                saved=(count-offset)%self.size
                if saved==0 and count and (self.value['pending'] is None or self.value['pending'] in history):saved=self.value['target_size']
                require_saved=history[max(0,count-saved):] if saved else []
                if len(require_saved)>self.value['target_size']:raise ValueError('旧文字任务保存进度需要人工核对')
                self.value['block_saved_start']=count-len(require_saved)
                self.value['saved_ids']=require_saved
                self.value['selected_ids']=list(dict.fromkeys(self.value['selected_ids']+require_saved))
                if self.value['pending'] in require_saved:self.value['pending']=None
                changed=True
        # Active outcomes require frozen refs. Legacy finished_tasks without
        # item proofs retain their separate visible-catalog compatibility check.
        valid={identifier for identifier,ref in refs.items() if ref['relative_path'] in completed}
        visible_valid={identifier for identifier,item in items.items() if item['relative_path'] in completed}
        if self.value['current_task'] is not None:
            kept=[identifier for identifier in self.value['completed_ids'] if identifier in valid or identifier not in refs]
            if kept!=self.value['completed_ids']:self.value['completed_ids']=kept;changed=True
        finished=[]
        for task in self.value['finished_tasks']:
            entries=[dict(snapshot=snapshot,index=i) for i in range(len(snapshot['items']))]
            task_ids={self.image(entry) for entry in entries if self.task(entry)==task}
            proof=self.value['finished_images'].get(task)
            if (proof is not None and (self.count_basis=='saved' or all(i in valid or i not in refs for i in proof))) or (proof is None and (not task_ids or task_ids<=visible_valid)):
                finished.append(task)
        if finished!=self.value['finished_tasks']:self.value['finished_tasks']=finished;changed=True
        proofs={task:images for task,images in self.value['finished_images'].items() if task in finished}
        if proofs!=self.value['finished_images']:self.value['finished_images']=proofs;changed=True
        if reserved_paths is not None:
            reserved=set(reserved_paths)
            kept=[identifier for identifier in self.value['selected_ids'] if identifier==self.value['pending']
                  or identifier in valid or identifier not in refs or refs[identifier]['relative_path'] in reserved
                  or (self.count_basis=='saved' and identifier in self.value['saved_ids'])]
            if kept!=self.value['selected_ids']:self.value['selected_ids']=kept;changed=True
            saved=[i for i in self.value['saved_ids'] if i in self.value['selected_ids']]
            if saved!=self.value['saved_ids']:self.value['saved_ids']=saved;changed=True
        if changed:self.save()
        pending=self.value['pending']
        ref=refs.get(pending)
        if ref and reserved_paths is not None and ref['relative_path'] in set(reserved_paths) and pending not in valid:
            self.defer(self._ref_entry(pending,ref))
        current=self.value['current_task']
        for identifier in list(self.value['selected_ids']):
            if self.value['current_task']!=current:break
            if identifier in valid and identifier not in self.value['completed_ids']:
                self.success(self._ref_entry(identifier,refs[identifier]))
        if self._finish_saved_block():self.save()

    @staticmethod
    def _ref_entry(identifier,ref):
        return dict(snapshot={'items':[dict(id=identifier,relative_path=ref['relative_path'],
                    source_lists=[{'id':ref['task']}])]},index=0)

    def choose(self,entries,choose_image):
        try:return self._choose(entries,choose_image)
        except BaseException as error:
            if not getattr(error,'committed',False):self.value=copy.deepcopy(self._saved)
            raise

    def _choose(self,entries,choose_image):
        if self.unresolved():raise ValueError('参考图关联需要恢复，请检查原始采集证据')
        groups={}
        for entry in entries:
            if self.round_robin and self.count_basis=='saved' and self.image(entry) in self.value['saved_history']:continue
            task=self.task(entry)
            if task:groups.setdefault(task,[]).append(entry)
        current=self.value['current_task']
        if self.size=='all' and current is not None and not groups.get(current):
            if self.awaiting_qc():raise ValueError('当前任务正在等待质检，请在结果返回后继续')
            self.value.update(current_task=None,target_size=None,completed_ids=[],pending=None,selected_ids=[],saved_ids=[]);current=None
        if current is None and self.round_robin:
            counts=self.round_counts()
            available=[task for task in self.ids if groups.get(task) and counts.get(task,0)<50]
            if not available:raise ValueError('本轮文字任务已完成，没有下一项任务')
            lowest=min(counts.get(task,0)//self.size for task in available)
            current=secrets.choice([task for task in available if counts.get(task,0)//self.size==lowest])
            target=min(self.size-counts.get(current,0)%self.size,50-counts.get(current,0),len(groups[current]))
            self.value.update(current_task=current,target_size=target,completed_ids=[],pending=None,selected_ids=[],saved_ids=[],block_saved_start=counts.get(current,0))
        if current is None:
            candidates=[task for task,rows in groups.items() if rows and task not in self.value['finished_tasks']]
            if not candidates:
                candidates=[task for task,rows in groups.items() if rows]
                if not candidates:raise ValueError('本轮任务已完成，没有下一项任务')
                last=self.value['finished_tasks'][-1] if self.value['finished_tasks'] else None
                if len(candidates)>1:candidates=[task for task in candidates if task!=last]
                self.value.update(finished_tasks=[],finished_images={})
            current=secrets.choice(candidates)
            target=len(groups[current]) if self.size=='all' else min(self.size,len(groups[current]))
            if self.size=='all' and hasattr(self,'_known_snapshot'):
                self.switch_task(current,self._known_snapshot,self._known_completed)
            else:self.value.update(current_task=current,target_size=target,completed_ids=[],pending=None,selected_ids=[],saved_ids=[])
        waiting=set(self.awaiting_qc())
        if self.count_basis=='saved':waiting.update(self.value['saved_ids'])
        if self.round_robin and self.count_basis=='saved':waiting.update(self.value['saved_history'])
        rows=[e for e in groups.get(current,[]) if self.image(e) not in set(self.value['completed_ids'])|waiting]
        if len(self.value['selected_ids'])>=self.value['target_size']:
            rows=[e for e in rows if self.image(e)==self.value['pending']]
        if not rows:
            if waiting:raise ValueError('当前任务块正在等待质检，请在结果返回后继续')
            raise ValueError('当前任务没有剩余图片，请检查任务库')
        if self.round_robin and self.value['pending'] is not None:
            chosen=next((e for e in rows if self.image(e)==self.value['pending']),None)
            if chosen is None:raise ValueError('本次任务尚未保存，请先恢复原任务')
        else:chosen=choose_image(rows)
        self.reserve(chosen)
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
        candidate=dict(previous,current_task=identifier,target_size=len(entries),completed_ids=done,pending=None,selected_ids=done,saved_ids=done,
                       finished_tasks=[v for v in previous['finished_tasks'] if v!=identifier],
                       finished_images={task:images for task,images in previous['finished_images'].items() if task!=identifier})
        self.value=candidate
        try:
            self.value['image_refs']=dict(previous['image_refs'])
            for entry in entries:
                if self.image(entry) in done:self._remember(entry)
            self.save()
        except Exception as error:
            if not getattr(error,'committed',False):self.value=previous
            raise
        return True

    def switch_task(self,identifier,snapshot,completed_paths):
        if self.unresolved():raise ValueError('参考图关联需要恢复，请检查原始采集证据')
        if identifier!=self.value['current_task'] and self.awaiting_qc():
            raise ValueError('当前任务正在等待质检，请在结果返回后切换')
        if self.size=='all':return self._switch_task_all(identifier,snapshot,completed_paths)
        if identifier not in self.ids:raise ValueError('任务不在本机场景中')
        rows=[item for item in snapshot['items'] if any(x['id']==identifier for x in item.get('source_lists',[]))]
        remaining=[item for item in rows if item['relative_path'] not in set(completed_paths)]
        if not rows:raise ValueError('这个任务列表未启用')
        if self.value['current_task']==identifier:return False
        if not remaining:raise ValueError('这个任务列表已完成')
        target=len(remaining) if self.size=='all' else min(self.size,len(remaining))
        self.value.update(current_task=identifier,target_size=target,completed_ids=[],pending=None,selected_ids=[],saved_ids=[],
            finished_tasks=[x for x in self.value['finished_tasks'] if x!=identifier],
            finished_images={task:images for task,images in self.value['finished_images'].items() if task!=identifier})
        self.save();return True

    def round_counts(self):
        counts={task:0 for task in self.ids}
        if self.count_basis=='saved':
            for identifier in self.value['saved_history']:
                task=self.value['image_refs'][identifier]['task']
                if task in counts:counts[task]+=1
        else:
            completed=set(getattr(self,'_known_completed',()))
            for i,item in enumerate(getattr(self,'_known_snapshot',{}).get('items',[])):
                task=self.task(dict(snapshot=self._known_snapshot,index=i))
                if item['relative_path'] in completed and task in counts:counts[task]+=1
        return counts

    def status(self):
        completed=len(self.value['saved_ids']) if self.count_basis=='saved' else len(self.value['completed_ids'])
        pending_paths=set(getattr(self,'_known_reserved',()))-set(getattr(self,'_known_completed',()))
        result=dict(selection_mode='task_until_exhausted' if self.size=='all' else 'task_blocks_random_without_replacement',block_size=self.value['target_size'],configured_block_size=self.size,
                    block_completed=completed,block_saved=len(self.value['saved_ids']),block_qualified=len(self.value['completed_ids']),
                    block_count_basis=self.count_basis,local_pending_qc=len({r['relative_path'] for r in self.value['image_refs'].values() if r['relative_path'] in pending_paths}),block_task_id=self.value['current_task'],
                    block_pending_qc=len(self.awaiting_qc()),recovery_required=bool(self.unresolved()),manual_task_switch=not self.round_robin,allowed_task_ids=list(self.ids))
        if self.round_robin:result.update(round_robin=True,task_completed=self.round_counts().get(self.value['current_task'],0),task_target=50)
        return result
