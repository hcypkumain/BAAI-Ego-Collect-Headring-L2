"""Signed PC retention receipts. Never delete on PC ingest time or failed upload."""
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import shutil
import time

from cloud_scope import dataset_path,object_key

PURPOSE='pc_cloud_confirmed_v1'
DAYS=3*86400

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+'.tmp')
    with temp.open('wb') as f:f.write(canonical(value));f.flush();os.fsync(f.fileno())
    temp.replace(path)
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

class Retention:
    def __init__(self,state_root):
        self.root=Path(state_root)
        if self.root.is_symlink():raise ValueError('Linked retention state directory')
        self.root.mkdir(parents=True,exist_ok=True);key=self.root/'signing-key'
        if key.is_symlink():raise ValueError('Linked retention signing key')
        if not key.exists():
            fd=os.open(str(key),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(os.urandom(32));f.flush();os.fsync(f.fileno())
        self.key=key.read_bytes()
        if len(self.key)!=32:raise ValueError('Invalid retention signing key')

    def seal(self,cloud_receipt,source,files,storage_uuid):
        dataset_path(cloud_receipt['dataset']);stamp=cloud_receipt.get('cloud_confirmed_at')
        if (cloud_receipt.get('cloud_state')!='remote_confirmed' or cloud_receipt.get('cloud_receipt_verified') is not True
                or type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0):
            raise ValueError('No verified cloud confirmation; PC retention cannot start')
        expected={object_key(cloud_receipt['dataset'],r['path']):r['bytes'] for r in files}
        actual={r['key']:r['bytes'] for r in cloud_receipt.get('objects',[])}
        if expected!=actual or cloud_receipt.get('files')!=len(files):raise ValueError('Cloud receipt is not the exact full package')
        value=dict(cloud_receipt,version=1,purpose=PURPOSE,source_path=str(source),local_manifest=files,
                   storage_uuid=storage_uuid,pc_delete_after=stamp+DAYS,retention_hold=False)
        value.pop('signature',None)
        value['signature']=hmac.new(self.key,canonical(value),hashlib.sha256).hexdigest()
        return value

    def verify(self,receipt):
        if not isinstance(receipt,dict) or receipt.get('purpose')!=PURPOSE or receipt.get('version')!=1:
            raise ValueError('Wrong PC retention receipt type')
        body={k:v for k,v in receipt.items() if k!='signature'}
        expected=hmac.new(self.key,canonical(body),hashlib.sha256).hexdigest()
        if not isinstance(receipt.get('signature'),str) or not hmac.compare_digest(expected,receipt['signature']):
            raise ValueError('PC retention receipt signature mismatch')
        dataset_path(receipt['dataset'])
        return receipt

    def related(self,cloud_receipt,source,files,family,lineage):
        self.verify(cloud_receipt)
        if family not in ('originals','archive','quarantine'):raise ValueError('Unknown related custody family')
        if not lineage.get('raw_handoff_receipt_sha256') or not lineage.get('archive_manifest_digest'):
            raise ValueError('Raw/standard lineage proof missing')
        value={k:v for k,v in cloud_receipt.items() if k!='signature'}
        value.update(source_path=str(source),local_manifest=files,family=family,lineage=lineage)
        value['signature']=hmac.new(self.key,canonical(value),hashlib.sha256).hexdigest();return value

    @staticmethod
    def _verify_files(root,files,partial=False):
        expected={r['path']:r for r in files};seen=set()
        for parent,dirs,names in os.walk(root,followlinks=False):
            if any(Path(parent,name).is_symlink() for name in dirs):raise ValueError('Linked expired package directory')
            for name in names:
                p=Path(parent,name);relative=p.relative_to(root).as_posix();row=expected.get(relative)
                if row is None or p.is_symlink() or not p.is_file() or p.stat().st_size!=row['bytes']:
                    raise ValueError('Expired package files differ; retaining data')
                digest=hashlib.sha256()
                with p.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
                if digest.hexdigest()!=row['sha256']:raise ValueError('Expired package SHA changed; retaining data')
                seen.add(relative)
        if not partial and seen!=set(expected):raise ValueError('Expired package incomplete; retaining data')

    def cleanup(self,receipt,data_root,storage_uuid,now=None):
        receipt=self.verify(receipt);now=time.time() if now is None else now
        stamp=receipt['cloud_confirmed_at'];deadline=stamp+DAYS
        if (receipt.get('cloud_state')!='remote_confirmed' or receipt.get('cloud_receipt_verified') is not True
                or receipt.get('retention_hold') is not False or receipt.get('pc_delete_after')!=deadline
                or not math.isfinite(now) or now<deadline):return dict(state='held',delete_after=deadline)
        if receipt['storage_uuid'].upper()!=storage_uuid.upper():raise ValueError('Retention volume identity changed')
        data_root=Path(data_root);source=Path(receipt['source_path']);family=receipt.get('family','standardized')
        if family not in ('standardized','originals','archive','quarantine'):raise ValueError('Unknown custody path family')
        standard=data_root/family
        relative=source.relative_to(standard)
        if '..' in relative.parts or source.resolve()!=source or standard.resolve()!=standard:
            raise ValueError('Retention path escaped the standard package root')
        fingerprint=hashlib.sha256(canonical(receipt)).hexdigest();journal=self.root/'deletions'/(fingerprint+'.json')
        previous=json.loads(journal.read_text()) if journal.exists() else None
        trash=data_root/'.pc-retention-trash'
        if trash.is_symlink():raise ValueError('Retention trash is linked')
        trash.mkdir(exist_ok=True);target=trash/fingerprint
        if previous and previous.get('state')=='deleted':return previous
        if source.exists() and target.exists():raise ValueError('Expired package name reused; retain both')
        if not previous:
            if not source.is_dir() or source.is_symlink():raise ValueError('Expired package missing without deletion journal')
            self._verify_files(source,receipt['local_manifest'])
            previous=dict(state='prepared',receipt_sha256=fingerprint,source=str(source),target=str(target),started_at=now)
            atomic(journal,previous)
        if source.exists():
            self._verify_files(source,receipt['local_manifest']);source.rename(target)
            if os.name!='nt':
                for directory in (source.parent,trash):
                    fd=os.open(directory,os.O_RDONLY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
        if target.exists():
            if target.is_symlink() or target.resolve().parent!=trash.resolve():raise ValueError('Retention deletion boundary invalid')
            self._verify_files(target,receipt['local_manifest'],partial=previous['state']=='deleting_files')
            previous['state']='deleting_files';atomic(journal,previous);shutil.rmtree(target)
        elif previous['state']!='deleting_files':raise ValueError('Retention target missing before committed deletion')
        previous.update(state='deleted',deleted_at=now);atomic(journal,previous);return previous
