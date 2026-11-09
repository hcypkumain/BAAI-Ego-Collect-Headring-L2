"""Bounded, session-bound scanner result envelope."""
import hashlib
import hmac
import json
import re

MAX_BYTES=262144


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def encode(run,nonce,request_id,fingerprint,rows):
    payload=dict(schema=1,run=run,nonce=nonce,request_id=request_id,fingerprint=fingerprint,rows=rows)
    value=dict(payload=payload,sha256=hashlib.sha256(canonical(payload)).hexdigest())
    data=canonical(value)
    if len(data)>MAX_BYTES:raise ValueError('扫描结果超过长度限制')
    return data.decode('utf-8')


def decode(data,run,nonce,request_id,fingerprint):
    if not isinstance(data,str) or len(data.encode('utf-8'))>MAX_BYTES:raise ValueError('扫描结果长度无效')
    def unique_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('扫描结果包含重复字段')
            value[key]=item
        return value
    try:value=json.loads(data,object_pairs_hook=unique_pairs)
    except (ValueError,TypeError) as exc:raise ValueError('扫描结果不完整') from exc
    if not isinstance(value,dict) or set(value)!={'payload','sha256'}:raise ValueError('扫描结果结构无效')
    payload=value['payload'];digest=value['sha256']
    if not isinstance(payload,dict) or not isinstance(digest,str):raise ValueError('扫描结果结构无效')
    if not hmac.compare_digest(digest,hashlib.sha256(canonical(payload)).hexdigest()):raise ValueError('扫描结果校验失败')
    if set(payload)!={'schema','run','nonce','request_id','fingerprint','rows'} or payload.get('schema')!=1:raise ValueError('扫描结果版本无效')
    if (payload.get('run')!=run or payload.get('nonce')!=nonce or payload.get('request_id')!=request_id
        or payload.get('fingerprint')!=fingerprint):raise ValueError('扫描结果所属请求或设备已变化')
    rows=payload.get('rows')
    if not isinstance(rows,list) or len(rows)>32:raise ValueError('扫描设备结果无效')
    if not isinstance(fingerprint,list):raise ValueError('扫描设备指纹无效')
    wrist_paths={item[1] for item in fingerprint if isinstance(item,list) and len(item)==5 and item[0]=='wrist' and isinstance(item[1],str)}
    names=[]
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('name'),str) or not row['name']
            or not isinstance(row.get('label'),str) or type(row.get('connected')) is not bool
            or row.get('status') not in ('ready','missing','no_data','error','timeout')):raise ValueError('扫描设备结果无效')
        if row['connected'] and row['status']!='ready':raise ValueError('扫描设备就绪状态矛盾')
        if row['connected'] and row['name'] in ('cam_left_wrist','cam_right_wrist') and row.get('device') not in wrist_paths:
            raise ValueError('腕部设备身份与扫描指纹不匹配')
        if row['connected'] and row['name'].startswith('cam_ros_') and (row.get('fresh_frame') is not True or row.get('probe_scope')!='camd_snapshot'):
            raise ValueError('头环新帧未确认')
        names.append(row['name'])
    if len(set(names))!=len(names):raise ValueError('扫描设备结果重复')
    return rows
