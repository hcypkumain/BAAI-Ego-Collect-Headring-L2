"""Validation for immutable UTF-8 text reference occurrences."""
import hashlib
import json
import re

NAMESPACE=re.compile(r'^temporary-[a-z0-9-]{1,80}$')
IDENTIFIER=re.compile(r'^text_[0-9a-f]{32}$')
MAX_BYTES=32768

def canonical(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')

def validate(value):
    keys={'version','namespace','source_sha256','task_id','scene','name','reference_text','occurrence','target_count'}
    if not isinstance(value,dict) or set(value)!=keys or type(value['version']) is not int or value['version']!=1:
        raise ValueError('文字任务格式无效')
    if not isinstance(value['namespace'],str) or not NAMESPACE.fullmatch(value['namespace']):raise ValueError('文字任务命名空间无效')
    if not isinstance(value['source_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['source_sha256']):raise ValueError('文字任务来源无效')
    if not isinstance(value['task_id'],str) or not IDENTIFIER.fullmatch(value['task_id']):raise ValueError('文字任务标识无效')
    for k,limit in (('scene',384),('name',384),('reference_text',16384)):
        text=value[k]
        if not isinstance(text,str) or not text.strip() or '\x00' in text or len(text.encode('utf-8'))>limit:raise ValueError('文字任务内容无效')
    if type(value['target_count']) is not int or not 1<=value['target_count']<=5000 or type(value['occurrence']) is not int or not 1<=value['occurrence']<=value['target_count']:
        raise ValueError('文字任务次数无效')
    return value

def read(raw):
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise ValueError('文字任务内容过大')
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result:raise ValueError('重复文字任务字段')
            result[key]=value
        return result
    value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    validate(value)
    if canonical(value)!=raw:raise ValueError('文字任务内容非规范格式')
    return value

def occurrence_id(namespace,task_id,ordinal):
    if not isinstance(namespace,str) or not NAMESPACE.fullmatch(namespace) or not isinstance(task_id,str) or not IDENTIFIER.fullmatch(task_id) or type(ordinal) is not int or not 1<=ordinal<=5000:raise ValueError('文字任务次数无效')
    return 'text_'+hashlib.sha256((namespace+':'+task_id+':'+str(ordinal)).encode('utf-8')).hexdigest()[:32]
