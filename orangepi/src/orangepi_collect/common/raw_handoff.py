"""Versioned raw custody proofs; a byte handoff is never a QC/cloud verdict."""
import hashlib
import hmac
import json
import math
from pathlib import PurePosixPath
import re

RAW_PURPOSE='raw_handoff_v1'
RESULT_PURPOSE='post_handoff_qc_v1'
RETENTION_SECONDS=3*24*60*60
SHA=re.compile(r'[0-9a-f]{64}\Z')
SESSION=re.compile(r'L[23]-[A-Za-z0-9_.-]{1,123}\Z')

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def sha_value(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def signed(value,key):
    if not key:raise ValueError('Signing key missing')
    result=dict(value)
    if 'signature' in result:raise ValueError('Already signed')
    result['signature']=hmac.new(key,canonical(result),hashlib.sha256).hexdigest()
    return result

def verify_signature(value,key,purpose):
    if not isinstance(value,dict) or value.get('purpose')!=purpose or value.get('version')!=1:
        raise ValueError('Wrong custody proof purpose/version')
    proof={k:v for k,v in value.items() if k!='signature'}
    signature=value.get('signature')
    if not isinstance(signature,str) or not SHA.fullmatch(signature) or not key:
        raise ValueError('Invalid custody signature')
    expected=hmac.new(key,canonical(proof),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,signature):raise ValueError('Custody signature mismatch')
    return proof

def relative_file(value):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid manifest path')
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path)!=value:
        raise ValueError('Manifest path escaped its root')
    return path

def validate_manifest(value):
    if not isinstance(value,dict) or not isinstance(value.get('files'),list) or not value['files']:
        raise ValueError('Empty custody manifest')
    names=[];total=0
    for row in value['files']:
        if not isinstance(row,dict):raise ValueError('Invalid manifest row')
        relative_file(row.get('path'));names.append(row['path'])
        if type(row.get('bytes')) is not int or row['bytes']<0:raise ValueError('Invalid file size')
        if not isinstance(row.get('sha256'),str) or not SHA.fullmatch(row['sha256']):
            raise ValueError('Missing file SHA256')
        total+=row['bytes']
    if names!=sorted(names) or len(set(names))!=len(names):raise ValueError('Duplicate/unsorted files')
    if (type(value.get('total_files')) is not int or value['total_files']!=len(names)
            or type(value.get('total_bytes')) is not int or value['total_bytes']!=total or total<=0):
        raise ValueError('Custody manifest totals mismatch')
    if value.get('digest')!=sha_value(value['files']):raise ValueError('Manifest digest mismatch')
    native=next((row for row in value['files'] if row['path']=='native_capture_summary.json'),None)
    if native is None or native['sha256']!=value.get('native_sha256'):
        raise ValueError('Native capture identity missing from manifest')
    return value

def same_file_bytes(left,right):
    """exFAT may reassign inode/mtime metadata; custody binds every actual byte."""
    validate_manifest(left);validate_manifest(right)
    keys=('path','bytes','sha256')
    return ([tuple(row[k] for k in keys) for row in left['files']]
        ==[tuple(row[k] for k in keys) for row in right['files']]
        and left['native_sha256']==right['native_sha256'])

def validate_raw_receipt(value,key,device_id,server_id,source_root):
    proof=verify_signature(value,key,RAW_PURPOSE)
    manifest=validate_manifest(proof.get('manifest'))
    sid=proof.get('session_id')
    if not isinstance(sid,str) or not SESSION.fullmatch(sid):raise ValueError('Invalid handoff session')
    if (proof.get('device_id')!=device_id or proof.get('server_id')!=server_id
            or manifest.get('device_id')!=device_id or manifest.get('session_id')!=sid
            or manifest.get('root')!=str(source_root)):
        raise ValueError('Handoff source/server identity mismatch')
    if proof.get('host_verified') is not True or proof.get('host_fsynced') is not True:
        raise ValueError('Host durable copy not committed')
    if not isinstance(proof.get('host_path'),str) or not PurePosixPath(proof['host_path']).is_absolute():
        raise ValueError('Host custody destination missing')
    if not isinstance(proof.get('host_storage_uuid'),str) or not proof['host_storage_uuid']:
        raise ValueError('Host custody storage identity missing')
    if (not isinstance(proof.get('transaction_id'),str)
            or not re.fullmatch(r'[0-9a-f]{32}',proof['transaction_id'])):
        raise ValueError('Invalid handoff transaction')
    return proof

def pc_retention_deadline(cloud_confirmed_at):
    if (type(cloud_confirmed_at) not in (int,float) or not math.isfinite(cloud_confirmed_at)
            or cloud_confirmed_at<=0):raise ValueError('Trusted cloud confirmation timestamp missing')
    return cloud_confirmed_at+RETENTION_SECONDS

def pc_cleanup_eligible(record,now,cloud_adapter_ready=False):
    """No PC deletion without trusted full cloud confirmation AND 72 hours."""
    if cloud_adapter_ready is not True or not isinstance(record,dict):return False
    if record.get('cloud_state')!='remote_confirmed' or record.get('cloud_receipt_verified') is not True:
        return False
    try:deadline=pc_retention_deadline(record.get('cloud_confirmed_at'))
    except (TypeError,ValueError):return False
    return (type(now) in (int,float) and math.isfinite(now) and now>=deadline
            and record.get('retention_hold') is False)
