"""Scoped adapter around the supplied SDK; no CLI prompts or optimistic success."""
import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from cloud_scope import BUCKET,dataset_path,object_key,scoped_key,scoped_prefix,validate_listing

SERVER='https://ei2rmd.baai.ac.cn'
ENDPOINT='ks3-cn-beijing.ksyuncs.com'
TOKEN='/api/eai/userAccessKey/getAccessToken'
STS='/api/eai/userAccessKey/getKs3AccessKey'
START='/api/eai/dataset/upload/start'
UPDATE='/api/eai/dataset/upload/process'
COMPLETE='/api/eai/dataset/upload/complete'

def digest_file(path,algorithm='sha256',chunk=1024*1024):
    digest=hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(chunk),b''):digest.update(block)
    return digest.hexdigest()

def expected_etag(path):
    size=Path(path).stat().st_size
    if size<=5*1024*1024:return digest_file(path,'md5')
    part_size=min(5*1024**3,max(5*1024**2,math.ceil(size/9999)))
    parts=[]
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(part_size),b''):parts.append(hashlib.md5(block).digest())
    return hashlib.md5(b''.join(parts)).hexdigest()+'-'+str(len(parts))

class CloudError(RuntimeError):pass

class _Progress:
    def update(self,*args):pass

class _ScopedBucket:
    def __init__(self,bucket,delete_keys):self._bucket=bucket;self._delete_keys=delete_keys
    def list(self,prefix=None,delimiter='',**kwargs):
        if isinstance(prefix,str) and prefix.endswith('/'):scoped_prefix(prefix)
        else:scoped_key(prefix)
        for key in self._bucket.list(prefix=prefix,delimiter=delimiter,**kwargs):
            if not key.name.startswith(prefix):raise CloudError('Storage returned out-of-prefix data')
            if key.name!=prefix:scoped_key(key.name)
            yield key
    def new_key(self,key,**kwargs):
        scoped_key(key);return self._bucket.new_key(key,**kwargs)
    def put_empty(self,key):
        scoped_key(key)
        # The supplied file-stream upload can wait indefinitely on an empty
        # stream. Send an explicit zero-length SDK PUT instead, then LIST-verify.
        response=self._bucket.connection.make_request('PUT',bucket=BUCKET,key=key,data=b'',
            headers={'Content-Length':'0','Content-MD5':'1B2M2Y8AsgTpgAmY7PhCfg==','If-None-Match':'*'},timeout=40)
        status=getattr(response,'status',getattr(response,'status_code',None))
        if hasattr(response,'close'):response.close()
        if status not in (200,201,204,412):raise CloudError('Empty-object PUT was not acknowledged')
    def get_all_multipart_uploads(self,prefix=None,**kwargs):
        scoped_key(prefix)
        uploads=list(self._bucket.get_all_multipart_uploads(prefix=prefix,**kwargs))
        for upload in uploads:
            if not upload.key_name.startswith(prefix):raise CloudError('Multipart lookup escaped requested prefix')
            scoped_key(upload.key_name)
        return uploads
    def initiate_multipart_upload(self,key,**kwargs):
        scoped_key(key);return self._bucket.initiate_multipart_upload(key,**kwargs)
    def delete_key(self,key,**kwargs):
        scoped_key(key,delete=True)
        if key not in self._delete_keys:raise CloudError('Deletion not in the explicit one-time EGOL2 snapshot')
        return self._bucket.delete_key(key,**kwargs)

class _ScopedConnection:
    def __init__(self,connection,delete_keys):self._connection=connection;self._delete_keys=delete_keys
    def get_bucket(self,name,**kwargs):
        if name!=BUCKET:raise CloudError('Storage bucket outside authorized scope')
        return _ScopedBucket(self._connection.get_bucket(name,**kwargs),self._delete_keys)

class ScopedUploader:
    def __init__(self,credential_path,state_root,proxy=None,delete_keys=(),authenticated_sdk=None,retry_unverified=False):
        # Dependencies are deliberately loaded only in the isolated SDK runtime.
        import requests
        from robot_data_uploader.uploader import RobotDataUploader
        from robot_data_uploader import config
        self.config=json.loads(Path(credential_path).read_text())
        if set(self.config.get('allowed_roots',[]))!={'EGOL2','EGOL3_hand'}:
            raise CloudError('Cloud credential scope configuration changed')
        scope=Path(credential_path).parent/'project-scope.json'
        self.project_scope=json.loads(scope.read_text()) if scope.is_file() else {}
        self.state_root=Path(state_root);self.state_root.mkdir(parents=True,exist_ok=True)
        self.retry_unverified=retry_unverified
        self.session=requests.Session();self.session.trust_env=False
        self.proxy=proxy
        if proxy:
            parsed=urlparse(proxy)
            if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost'):
                raise CloudError('Only explicit loopback egress proxy accepted')
            self.session.proxies={'http':proxy,'https':proxy}
        self.session.verify=True
        config.SERVER_URL=SERVER;config.ENDPOINT=ENDPOINT;config.BUCKET_NAME=BUCKET;config.UPLOAD_TARGET='data'
        self.sdk=authenticated_sdk if authenticated_sdk is not None else RobotDataUploader()
        self.sdk.resume_dir=str(self.state_root/'resume')
        Path(self.sdk.resume_dir).mkdir(exist_ok=True)
        self.delete_keys=frozenset(delete_keys)
        for key in self.delete_keys:scoped_key(key,delete=True)
        self.sdk.get_connection=self._connection
        self.sdk.get_ks3_sts=self.refresh_sts
        self.token=None;self.sts=None;self.acquired=0
        if authenticated_sdk is None:self.authenticate()
        else:
            self.token=self.sdk.eai_token;self.sts=self.sdk.sts_token
            if not self.token or not self.sts:raise CloudError('CLI did not authenticate the supplied SDK')
            self.acquired=getattr(self.sdk,'sts_acquired_at',time.time())
            self.sdk.connection=None

    def _api(self,path,body=None,bearer=False):
        if path not in (TOKEN,STS,START,UPDATE,COMPLETE):raise CloudError('Platform API outside upload protocol')
        if path==START:
            dataset_path(body.get('target_root_path'));dataset_path(body.get('target_full_path'))
            if body['target_root_path']!=body['target_full_path']:raise CloudError('Upload target changed')
        headers={}
        if self.token:headers['Authorization']=('Bearer ' if bearer else '')+self.token
        response=self.session.request('POST' if body is not None else 'GET',SERVER+path,
            json=body,headers=headers,timeout=(15,40),allow_redirects=False)
        if response.status_code!=200:raise CloudError('Platform upload API HTTP '+str(response.status_code))
        value=response.json()
        if value.get('code')!=200:
            message=str(value.get('msg',''))[:200]
            for secret in [self.config.get('access_key'),self.config.get('secret_key'),self.token]:
                if secret:message=message.replace(secret,'[redacted]')
            raise CloudError('Platform upload API code '+str(value.get('code'))+': '+message)
        return value.get('data')

    def authenticate(self):
        result=self._api(TOKEN,{'ak':self.config['access_key'],'sk':self.config['secret_key']})
        self.token=result['token'];self.sdk.eai_token=self.token;self.sdk.eai_task_id=-99
        self.refresh_sts()

    def refresh_sts(self):
        self.sts=self._api(STS,bearer=True);self.acquired=time.time()
        self.sdk.sts_token=self.sts;self.sdk.sts_acquired_at=self.acquired;self.sdk.connection=None
        return True

    def _connection(self,show_progress=False):
        from ks3.connection import Connection
        if self.sdk.connection is None:
            kwargs={}
            if self.proxy:
                proxy=urlparse(self.proxy);kwargs=dict(proxy_host=proxy.hostname,proxy_port=proxy.port)
            connection=Connection(access_key_id=self.sts['accessKeyId'],access_key_secret=self.sts['secretAccessKey'],
                security_token=self.sts['securityToken'],host=ENDPOINT,is_secure=True,debug=0,
                enable_crc=True,timeout=40,dns_cache_time=0,**kwargs)
            connection.session.verify=True
            self.sdk.connection=_ScopedConnection(connection,self.delete_keys)
        return self.sdk.connection

    def inventory(self,prefix):
        scoped_prefix(prefix)
        if time.time()-self.acquired>2400:self.refresh_sts()
        rows=[dict(key=key.name,bytes=int(key.size),etag=str(key.etag or '').strip('"'))
              for key in self._connection().get_bucket(BUCKET).list(prefix=prefix,delimiter='')]
        return validate_listing(rows,prefix)

    def begin(self,root,target,files):
        dataset_path(target)
        name=target.split('/')[0];binding=self.project_scope.get(name,{})
        if binding.get('project_name')!=name or type(binding.get('project_id')) is not int:
            raise CloudError('CLI-verified project binding missing for '+name)
        data=dict(eai_task_id=-99,task_name=target,source_path=str(root),target_root_path=target,
            target_full_path=target,total_file_count=len(files),total_size_bytes=sum(f['bytes'] for f in files),
            project_id=binding['project_id'],data_format='LerobotDataset3.1')
        result=self._api(START,data)
        if not isinstance(result,dict) or not result.get('uploadTaskId'):raise CloudError('Upload task acknowledgement missing')
        return result['uploadTaskId']

    def _object_proof_path(self,key):
        directory=self.state_root/'object-proofs';directory.mkdir(exist_ok=True)
        return directory/(hashlib.sha256(key.encode()).hexdigest()+'.json')

    @staticmethod
    def _atomic_json(path,value):
        temporary=path.with_name(path.name+'.tmp')
        with temporary.open('w') as f:json.dump(value,f,allow_nan=False);f.flush();os.fsync(f.fileno())
        temporary.replace(path)

    @staticmethod
    def _crc(path):
        from ks3.utils import Crc64
        crc=Crc64()
        with Path(path).open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):crc.update(block)
        return str(crc.crc)

    def _multipart_verified(self,path,key,row):
        from filechunkio import FileChunkIO
        from ks3.multipart import PartInfo
        from ks3.utils import Crc64
        bucket=self._connection().get_bucket(BUCKET);part_size=min(5*1024**3,max(5*1024**2,math.ceil(row['bytes']/9999)))
        intent_path=self._object_proof_path(key).with_suffix('.intent.json')
        intent=json.loads(intent_path.read_text()) if intent_path.exists() else {}
        uploads=bucket.get_all_multipart_uploads(prefix=key)
        upload=next((u for u in uploads if u.key_name==key and u.id==intent.get('upload_id')
                     and intent.get('sha256')==row['sha256'] and intent.get('bytes')==row['bytes']),None)
        if upload is None:
            upload=bucket.initiate_multipart_upload(key,headers={'x-kss-storage-class':'STANDARD'})
            self._atomic_json(intent_path,dict(key=key,sha256=row['sha256'],bytes=row['bytes'],upload_id=upload.id))
        existing={part.part_number:part for part in upload}
        expected_crc=self._crc(path)
        for number in range(1,math.ceil(row['bytes']/part_size)+1):
            offset=(number-1)*part_size;count=min(part_size,row['bytes']-offset)
            with Path(path).open('rb') as f:f.seek(offset);data=f.read(count)
            if len(data)!=count:raise CloudError('Local multipart source shortened')
            crc=Crc64();crc.update(data);md5=hashlib.md5(data).hexdigest();part=existing.get(number)
            if part is not None and part.size==count and str(part.etag).strip('"')==md5:
                # ListParts MD5 proves these source bytes; restore SDK CRC state
                # rather than inventing a missing server checksum or duplicating parts.
                upload.part_crc_infos[number]=PartInfo(count,str(crc.crc));continue
            with FileChunkIO(str(path),'r',offset=offset,bytes=count) as f:
                response=upload.upload_part_from_file(f,part_num=number)
            server_crc=response.getheader('x-kss-checksum-crc64ecma')
            if server_crc is None or str(server_crc)!=str(crc.crc):raise CloudError('Multipart part CRC64 verification failed')
        response=upload.complete_upload()
        server_crc=getattr(response,'ChecksumCRC64ECMA',None)
        if server_crc is None or str(server_crc)!=expected_crc:raise CloudError('Complete object CRC64 verification failed')
        return expected_crc

    def upload_file(self,root,target,row):
        dataset_path(target);path=Path(root)/row['path'];path.resolve().relative_to(Path(root).resolve())
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes']:
            raise CloudError('Local package file missing or changed')
        if digest_file(path)!=row['sha256']:raise CloudError('Local package SHA changed')
        expected=object_key(target,row['path']);etag=expected_etag(path);large=row['bytes']>5*1024*1024
        proof_path=self._object_proof_path(expected)
        proof=json.loads(proof_path.read_text()) if proof_path.exists() else {}
        existing=[k for k in self._connection().get_bucket(BUCKET).list(prefix=expected,delimiter='') if k.name==expected]
        if existing:
            actual_etag=str(existing[0].etag).strip('"')
            cached_large=(large and proof.get('sha256')==row['sha256'] and proof.get('bytes')==row['bytes']
                          and proof.get('etag')==actual_etag and proof.get('server_crc64')==self._crc(path))
            if len(existing)!=1 or int(existing[0].size)!=row['bytes']:
                raise CloudError('Existing cloud object differs; refusing silent overwrite')
            if cached_large or not large and actual_etag==etag:
                if not cached_large:
                    self._atomic_json(proof_path,dict(key=expected,bytes=row['bytes'],sha256=row['sha256'],
                        etag=actual_etag,server_crc64=None,verification='MD5_ETag'))
                return dict(key=expected,bytes=row['bytes'],etag=actual_etag,skipped=True)
            if not large or not self.retry_unverified:
                raise CloudError('Existing object has no validated integrity receipt; refusing overwrite')
        # Do not let the SDK's console leak credentials on exceptional replies.
        captured=io.StringIO()
        server_crc=None
        with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(captured):
            if row['bytes']==0:
                self._connection().get_bucket(BUCKET).put_empty(expected);result=(0,expected)
            elif large:
                server_crc=self._multipart_verified(path,expected,row);result=(row['bytes'],expected)
            else:
                result=self.sdk.upload_file(str(path),target,base_dir=str(root),skip_dir_check=True,
                    show_progress=False,pbar=_Progress())
        if not isinstance(result,tuple) or result[1]!=expected or result[0]!=row['bytes']:
            raise CloudError('SDK did not acknowledge the exact object and size')
        if digest_file(path)!=row['sha256']:raise CloudError('Local package changed during upload')
        actual=[k for k in self._connection().get_bucket(BUCKET).list(prefix=expected,delimiter='') if k.name==expected]
        if len(actual)!=1 or int(actual[0].size)!=row['bytes'] or not large and str(actual[0].etag).strip('"')!=etag:
            raise CloudError('Remote object size/ETag integrity verification failed')
        actual_etag=str(actual[0].etag).strip('"')
        verified=dict(key=expected,bytes=row['bytes'],sha256=row['sha256'],etag=actual_etag,
                      server_crc64=server_crc,verification='SDK_server_CRC64' if large else 'MD5_ETag')
        self._atomic_json(proof_path,verified)
        return dict(verified,skipped=False)

    def confirm(self,task_id,target,files):
        dataset_path(target);prefix='data/'+target+'/'
        actual=self.inventory(prefix)
        expected={object_key(target,row['path']):row['bytes'] for row in files}
        if {row['key']:row['bytes'] for row in actual}!=expected:
            raise CloudError('Cloud package object set/count/sizes incomplete')
        integrity=[]
        for row in files:
            key=object_key(target,row['path']);path=self._object_proof_path(key)
            if not path.is_file():raise CloudError('Per-file integrity receipt missing')
            proof=json.loads(path.read_text())
            if proof.get('key')!=key or proof.get('sha256')!=row['sha256'] or proof.get('bytes')!=row['bytes']:
                raise CloudError('Per-file integrity receipt differs from frozen package')
            live=next(item for item in actual if item['key']==key)
            if proof.get('etag')!=live['etag']:raise CloudError('Cloud object changed after file verification')
            integrity.append(proof)
        self._api(UPDATE,dict(upload_task_id=task_id,success_file_count=len(files),failed_file_count=0,
                             data_format='LerobotDataset3.1'))
        self._api(COMPLETE,dict(upload_task_id=task_id,status='SUCCESS'))
        confirmed=time.time()
        return dict(version=1,dataset=target,upload_task_id=task_id,cloud_state='remote_confirmed',
            cloud_receipt_verified=True,cloud_confirmed_at=confirmed,pc_delete_after=confirmed+259200,
            retention_hold=False,
            files=len(files),bytes=sum(row['bytes'] for row in files),objects=actual,integrity_receipts=integrity,
            verification='exact_object_set_sizes_and_per_file_MD5_or_SDK_server_CRC64',
            remote_sha256_not_claimed=True)

    def delete_snapshot_key(self,row):
        key=scoped_key(row['key'],delete=True)
        if key not in self.delete_keys:raise CloudError('Key not in authorized deletion snapshot')
        actual=[k for k in self._connection().get_bucket(BUCKET).list(prefix=key,delimiter='') if k.name==key]
        if not actual:return dict(key=key,already_absent=True)
        if len(actual)!=1 or int(actual[0].size)!=row['bytes'] or str(actual[0].etag).strip('"')!=row['etag']:
            raise CloudError('Old cloud object changed since deletion inventory; retained')
        self._connection().get_bucket(BUCKET).delete_key(key,headers={'If-Match':'"'+row['etag']+'"'})
        return dict(key=key,deleted=True)
