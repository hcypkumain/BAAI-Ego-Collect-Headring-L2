"""Append a scoped temporary text-task pool using the catalog transaction."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from reference_catalog import MAX_IMAGES,MAX_LISTS,CatalogError,_sync_directory,valid_id,display_name
from text_reference import canonical,validate,occurrence_id


def _load_manifest(raw):
    def pairs(rows):
        value={}
        for key,item in rows:
            if key in value:raise ValueError('duplicate manifest key')
            value[key]=item
        return value
    if not isinstance(raw,bytes) or len(raw)>2_000_000:raise ValueError('invalid manifest size')
    data=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    keys={'schema_version','namespace','source_sha256','temporary','task_count','target_per_task','total_target','tasks'}
    if not isinstance(data,dict) or set(data)!=keys or type(data['schema_version']) is not int or data['schema_version']!=1 or data['temporary'] is not True:raise ValueError('invalid manifest')
    namespace=data['namespace'];source=data['source_sha256'];tasks=data['tasks']
    if not isinstance(namespace,str) or not re.fullmatch(r'temporary-[a-z0-9-]{1,80}',namespace) or not isinstance(source,str) or not re.fullmatch(r'[0-9a-f]{64}',source):raise ValueError('invalid source')
    if not isinstance(tasks,list) or not 1<=len(tasks)<=100 or type(data['task_count']) is not int or data['task_count']!=len(tasks):raise ValueError('invalid task count')
    if type(data['target_per_task']) is not int or not 1<=data['target_per_task']<=100 or type(data['total_target']) is not int or data['total_target']!=len(tasks)*data['target_per_task']:raise ValueError('invalid target count')
    seen=set()
    for t in tasks:
        if not isinstance(t,dict) or set(t)!={'id','scene','name','reference_text','target_count'} or t['id'] in seen or t['target_count']!=data['target_per_task']:raise ValueError('invalid task')
        validate(dict(version=1,namespace=namespace,source_sha256=source,task_id=t['id'],scene=t['scene'],name=t['name'],reference_text=t['reference_text'],occurrence=1,target_count=t['target_count']))
        seen.add(t['id'])
    return data


def _id(prefix,namespace,value):return prefix+hashlib.sha256((namespace+':'+value).encode('utf-8')).hexdigest()[:32]


def append(catalog,raw,expected_revision,require_catalog_absent=False):
    data=_load_manifest(raw);namespace=data['namespace'];revision=expected_revision
    if require_catalog_absent and (catalog.path.exists() or catalog.path.is_symlink()):raise ValueError('脑电图库已出现，请重新核查并使用追加计划')
    old=copy.deepcopy(catalog._load())
    manifest_path=catalog._resolve('_uploads/_temporary_text/'+namespace+'/manifest.json')
    if old['revision']!=revision:raise CatalogError('图库已更新，请刷新后重试','revision_conflict',409)
    if any(catalog.pins.values()):raise ValueError('采集中不能导入文字任务')
    pool_id=_id('list_',namespace,'pool');task_lists=[];assets=[];ids=set()
    for task in data['tasks']:
        list_id=_id('list_',namespace,task['id']);instances=[]
        for n in range(1,task['target_count']+1):
            asset_id=occurrence_id(namespace,task['id'],n);filename='occurrence_%04d.text.json'%n
            path='_uploads/_temporary_text/'+namespace+'/'+task['id']+'/'+filename
            content=canonical(validate(dict(version=1,namespace=namespace,source_sha256=data['source_sha256'],task_id=task['id'],scene=task['scene'],name=task['name'],reference_text=task['reference_text'],occurrence=n,target_count=task['target_count'])))
            assets.append((asset_id,path,filename,display_name(task['name']),content));instances.append(asset_id);ids.add(asset_id)
        task_lists.append(dict(id=list_id,name=display_name(task['scene']+'_'+task['name']),items=instances,readonly=False,deleted=False,hidden=True,sampling_enabled=False,tags=['文字任务','脑电'],text_task_namespace=namespace))
    all_lists=task_lists+[dict(id=pool_id,name='临时洗衣区脑电文字任务',items=[a[0] for a in assets],readonly=False,deleted=False,hidden=False,sampling_enabled=False,tags=['文字任务','脑电'],text_task_namespace=namespace,text_task_list_ids=[x['id'] for x in task_lists])]
    existing=[x for x in all_lists if x['id'] in old['lists']]
    if existing:
        if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.read_bytes()!=raw:raise ValueError('临时任务导入清单不一致')
        if len(existing)!=len(all_lists) or any(old['lists'][x['id']]!=x for x in all_lists):raise ValueError('临时任务命名空间已被修改')
        for identifier,path,filename,name,content in assets:
            item=old['images'].get(identifier)
            if not item or item.get('path')!=path or item.get('sha256')!=hashlib.sha256(content).hexdigest() or catalog._resolve(path).read_bytes()!=content:raise ValueError('临时任务内容不一致')
        return {'namespace':namespace,'pool_id':pool_id,'tasks':len(task_lists),'occurrences':len(assets),'revision':revision,'already':True}
    if len(old['images'])+len(assets)>MAX_IMAGES or len(old['lists'])+len(all_lists)>MAX_LISTS:raise ValueError('图库容量不足')
    # Install only immutable new names; an interrupted pre-publication import is safely retryable.
    manifest_path.parent.mkdir(parents=True,exist_ok=True);catalog._resolve('_uploads/_temporary_text/'+namespace+'/manifest.json')
    try:
        fd=os.open(str(manifest_path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    except FileExistsError:
        if manifest_path.is_symlink() or manifest_path.read_bytes()!=raw:raise ValueError('临时任务导入清单冲突')
    _sync_directory(manifest_path.parent)
    for identifier,path,filename,name,content in assets:
        target=catalog._resolve(path);target.parent.mkdir(parents=True,exist_ok=True);catalog._resolve(path)
        try:
            fd=os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
            with os.fdopen(fd,'wb') as f:f.write(content);f.flush();os.fsync(f.fileno())
        except FileExistsError:
            if target.is_symlink() or target.read_bytes()!=content:raise ValueError('临时任务文件冲突')
        _sync_directory(target.parent)
    with catalog._transaction(revision) as value:
        if require_catalog_absent and (catalog.path.exists() or catalog.path.is_symlink()):raise ValueError('脑电图库已出现，请重新核查并使用追加计划')
        if any(catalog.pins.values()):raise ValueError('采集中不能导入文字任务')
        if any(identifier in value['images'] for identifier in ids) or any(x['id'] in value['lists'] for x in all_lists):raise ValueError('临时任务标识冲突')
        for identifier,path,filename,name,content in assets:
            value['images'][identifier]=dict(id=identifier,name=name,path=path,kind='text',origin='upload',deleted=False,original_filename=filename,size_bytes=len(content),sha256=hashlib.sha256(content).hexdigest())
            value['image_order'].append(identifier)
        for item in all_lists:value['lists'][item['id']]=item;value['list_order'].append(item['id'])
    return {'namespace':namespace,'pool_id':pool_id,'tasks':len(task_lists),'occurrences':len(assets),'revision':catalog.revision,'already':False}


def retire(catalog,namespace,expected_revision):
    if not isinstance(namespace,str) or not re.fullmatch(r'temporary-[a-z0-9-]{1,80}',namespace):raise ValueError('invalid namespace')
    with catalog._transaction(expected_revision) as value:
        rows=[row for row in value['lists'].values() if row.get('text_task_namespace')==namespace]
        if not rows:raise ValueError('找不到临时任务池')
        if any(catalog.pins.get(identifier) for row in rows for identifier in row['items']):raise ValueError('临时任务正在采集，不能撤销')
        for row in rows:row.update(deleted=True,hidden=True,sampling_enabled=False)
    return {'namespace':namespace,'lists':len(rows),'revision':catalog.revision}
