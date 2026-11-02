"""One upload or cleanup process. Credentials arrive only through stdin."""
import base64,contextlib,hashlib,io,json,os,shutil,signal,sys,time
from pathlib import Path
from cloud_common import CloudError,atomic,read_json,source_manifest,check_source,verify_remote,same_manifest,session_lock,sync_dir

SERVER='https://ei2rmd.baai.ac.cn'

def emit(**value):
    sys.__stdout__.write(json.dumps(value,ensure_ascii=False)+'\n');sys.__stdout__.flush()

def authenticate(credentials):
    with open(os.devnull,'w') as silent,contextlib.redirect_stdout(silent),contextlib.redirect_stderr(silent):
        from robot_data_uploader import uploader as vendor,config
    config.SERVER_URL=SERVER;config.ENDPOINT=config.ENDPOINT_BACKUP
    original=vendor.Connection
    def secure(*args,**kwargs):
        kwargs.update(is_secure=True,port=443,timeout=20);return original(*args,**kwargs)
    vendor.Connection=secure
    client=vendor.RobotDataUploader();client.set_max_worker(2);client.set_file_filters(['*'])
    if not client.get_eai_token(credentials['ak'],credentials['sk']):raise CloudError('AK/SK 验证失败或平台暂不可达，请核对凭据后重试')
    # Platform login and KS3 object-storage authorization are separate steps.
    # The wheel's get_connection() exits if sts_token has never been populated.
    if not client.get_ks3_sts():raise CloudError('平台临时存储授权获取失败，未开始传输，请稍后重试')
    token=getattr(client,'sts_token',None)
    if not isinstance(token,dict) or any(not isinstance(token.get(key),str) or not token[key] for key in ('accessKeyId','secretAccessKey','securityToken')):
        raise CloudError('平台临时存储授权不完整，未开始传输，请稍后重试')
    return client,vendor,config

class Progress:
    def __init__(self,total,callback):self.total=total;self.value=0;self.callback=callback;self.last=0
    def update(self,number):
        self.value=min(self.total,max(0,self.value+int(number)))
        if time.monotonic()-self.last>.5:self.last=time.monotonic();self.callback(self.value)
    def refresh(self,*args,**kwargs):pass
    def close(self):pass

def upload_empty(client,config,key):
    # Requests treats an empty file object as a stream and adds chunked
    # encoding while this SDK also forces Content-Length: 0. Send a literal
    # empty payload through the SDK's signed request path instead.
    client._refresh_sts_if_needed();connection=client.get_connection(False)
    headers={'Content-Length':'0','Content-Type':'application/octet-stream',
             'Content-MD5':base64.b64encode(hashlib.md5(b'').digest()).decode('ascii')}
    response=connection.make_request('PUT',config.BUCKET_NAME,key,data=b'',headers=headers)
    if not response or not 200<=response.status<300:raise CloudError('零字节文件上传未获存储服务确认，本地数据保留')
    response.read()
    # Upload-only STS grants can allow PUT/LIST while denying object HEAD.
    # Use the same exact-name/size evidence as the final full verification.
    stored=[row for row in connection.get_bucket(config.BUCKET_NAME).list(prefix=key,delimiter='') if row.name==key]
    if len(stored)!=1 or int(stored[0].size)!=0:raise CloudError('零字节文件云端核验未通过，本地数据保留')

def upload(payload):
    base=Path(payload['base']);job=payload['job'];source=Path(job['source']);directory=Path(payload['directory'])
    if source.resolve().parent!=Path(job['allowed_root']).resolve() or source.name!=job['session_id']:raise CloudError('上传目录越界')
    with session_lock(base,job['session_id']):
        emit(state='checking',phase='校验本地文件')
        manifest_path=directory/'manifest.json';manifest=read_json(manifest_path)
        if manifest is None:
            manifest=source_manifest(source);atomic(manifest_path,manifest)
        else:check_source(source,manifest)
        emit(total_files=manifest['total_files'],total_bytes=manifest['total_bytes'],completed_files=0,completed_bytes=0)
        fingerprint=hashlib.sha256(payload['credentials']['ak'].encode()).hexdigest()
        attempt=read_json(directory/'platform.json',{})
        if attempt and attempt.get('account')!=fingerprint:raise CloudError('本批次已绑定另一组平台凭据，请使用原上传账号重试')
        client,vendor,config=authenticate(payload['credentials'])
        projects=client.get_project_list() or []
        matches=[row for row in projects if str(row.get('projectName','')).strip().casefold()==job['settings']['project_name'].strip().casefold()]
        if len(matches)!=1:raise CloudError('找不到唯一匹配的授权项目，请核对项目名称')
        client.project_id=matches[0]['projectId'];client.data_format=vendor.detect_data_format(str(source))
        if not client.get_eai_task(job['settings']['task_id'] if job['settings']['task_id'] is not None else -99):raise CloudError('平台任务 ID 无效或无权限')
        bucket=client.get_connection(False).get_bucket(config.BUCKET_NAME)
        prefix=config.UPLOAD_TARGET+'/'+job['remote_path']+'/'
        remote=list(bucket.list(prefix=prefix,delimiter=''))
        if not attempt:
            if remote:raise CloudError('目标目录已有数据，本次未覆盖；请联系维护人员核对')
            data=dict(eai_task_id=client.eai_task_id,task_name=job['remote_path'],source_path=str(source),target_root_path=job['remote_path'],
                target_full_path=job['remote_path'],total_file_count=manifest['total_files'],total_size_bytes=manifest['total_bytes'],project_id=client.project_id)
            if not client.beigin_upload_eai_task(data) or not client.eai_upload_task_id:raise CloudError('平台尚未确认创建上传任务，未开始传输')
            attempt=dict(account=fingerprint,upload_task_id=client.eai_upload_task_id,project_id=client.project_id,manifest_digest=manifest['digest'])
            atomic(directory/'platform.json',attempt)
        else:
            if attempt['manifest_digest']!=manifest['digest'] or attempt['project_id']!=client.project_id:raise CloudError('原上传任务与当前文件或项目不一致')
            client.eai_upload_task_id=attempt['upload_task_id']
        expected={prefix+row['path']:row['bytes'] for row in manifest['files']}
        remote_map={row.name:int(row.size) for row in remote}
        if set(remote_map)-set(expected):raise CloudError('目标目录存在本清单以外的文件，已停止以避免混入其他批次')
        completed=0;done_bytes=0;last_platform=0
        emit(state='uploading',phase='上传文件',platform_task_id=client.eai_upload_task_id)
        # A previous worker was interrupted: identical manifest and account are
        # required before the vendor's multipart continuation can be reused.
        for row in manifest['files']:
            key=prefix+row['path'];path=source/row['path']
            emit(current_file=row['path'],completed_files=completed,completed_bytes=done_bytes)
            if remote_map.get(key)!=row['bytes']:
                if row['bytes']==0:upload_empty(client,config,key)
                else:
                    adapter=Progress(row['bytes'],lambda n:emit(completed_bytes=done_bytes+n))
                    returned=client.upload_file(str(path),job['remote_path'],base_dir=str(source),skip_dir_check=True,show_progress=False,pbar=adapter)
                    if not isinstance(returned,tuple) or len(returned)!=2 or returned[1]!=key:raise CloudError('上传器未确认文件成功，已停止并保留本地数据')
            completed+=1;done_bytes+=row['bytes']
            emit(completed_files=completed,completed_bytes=done_bytes)
            if time.monotonic()-last_platform>10:
                client.update_upload_eai_task_progress(dict(upload_task_id=client.eai_upload_task_id,success_file_count=completed,failed_file_count=0,data_format=client.data_format));last_platform=time.monotonic()
        emit(state='verifying',phase='核验云端文件与本地清单',current_file='')
        client._refresh_sts_if_needed();bucket=client.get_connection(False).get_bucket(config.BUCKET_NAME)
        verified=verify_remote(manifest,prefix,bucket.list(prefix=prefix,delimiter=''))
        check_source(source,manifest)
        if not client.update_upload_eai_task_progress(dict(upload_task_id=client.eai_upload_task_id,success_file_count=completed,failed_file_count=0,data_format=client.data_format)):
            raise CloudError('文件已传输，但平台尚未确认最终进度；重试核验后才可清理')
        if not client.complete_upload_eai_task('SUCCESS'):raise CloudError('文件已核验，但平台尚未确认上传完成；暂不可清理')
        receipt=dict(version=1,status='success',session_id=job['session_id'],job_id=job['id'],remote_path=job['remote_path'],
                     manifest_digest=manifest['digest'],platform_task_id=client.eai_upload_task_id,project_id=client.project_id,
                     platform_completion_acknowledged=True,verification=verified,account=fingerprint,completed_at=time.time())
        atomic(directory/'receipt.json',receipt)
        emit(state='success',phase='已上传并核验，可清理本地数据',completed_at=receipt['completed_at'],receipt=receipt,
             completed_files=manifest['total_files'],completed_bytes=manifest['total_bytes'],current_file='')

def cleanup(payload):
    job=payload['job'];source=Path(job['source']);directory=Path(payload['directory']);base=Path(payload['base'])
    with session_lock(base,job['session_id']):
        manifest=read_json(directory/'manifest.json');receipt=read_json(directory/'receipt.json')
        if not receipt or receipt.get('status')!='success' or receipt.get('manifest_digest')!=manifest.get('digest') or not receipt.get('platform_completion_acknowledged'):
            raise CloudError('没有完整云端成功回执，禁止删除本地数据')
        if source.resolve().parent!=Path(job['allowed_root']).resolve() or source.name!=job['session_id']:raise CloudError('清理目录越界')
        intent_path=directory/'cleanup.json';intent=read_json(intent_path)
        if intent is None:
            check_source(source,manifest)
            volume=Path(job['volume']).resolve();trash=volume/'uploaded-cleanup';trash.mkdir(exist_ok=True)
            if trash.is_symlink() or trash.resolve().parent!=volume:raise CloudError('清理目录无效')
            destination=trash/(job['session_id']+'--'+job['id'])
            intent=dict(source=str(source),destination=str(destination),phase='prepared');atomic(intent_path,intent)
        destination=Path(intent['destination'])
        if destination.parent.resolve()!=Path(job['volume']).resolve()/'uploaded-cleanup':raise CloudError('清理恢复路径无效')
        if source.exists():
            check_source(source,manifest)
            if destination.exists():raise CloudError('清理事务目录冲突')
            os.rename(str(source),str(destination));sync_dir(source.parent);sync_dir(destination.parent)
        emit(state='cleaning',phase='删除已核验的本地副本')
        if destination.exists():shutil.rmtree(str(destination));sync_dir(destination.parent)
        intent.update(phase='complete',completed_at=time.time());atomic(intent_path,intent)
        # The reference ledger and receipt are intentionally retained. This is
        # storage cleanup, not invalidating/deleting a failed recording.
        emit(state='success',phase='云端已上传，本地副本已清理',local_deleted=True,cleaned_at=time.time())

def main():
    payload=json.loads(sys.stdin.readline());mode=payload.get('mode','upload');directory=Path(payload['directory'])
    directory.mkdir(parents=True,exist_ok=True);os.chdir(str(directory));os.umask(0o077)
    # Never forward native stdout/stderr: upstream logs can contain tokens.
    with open(os.devnull,'w') as silent,contextlib.redirect_stdout(silent),contextlib.redirect_stderr(silent):
        if mode=='cleanup':cleanup(payload)
        else:upload(payload)

if __name__=='__main__':
    try:main()
    except Exception as error:
        message=str(error) if isinstance(error,CloudError) else '操作未完成（%s），请查看批次状态并重试'%type(error).__name__
        emit(state='failed',phase='需要重试',error=message);sys.exit(1)
