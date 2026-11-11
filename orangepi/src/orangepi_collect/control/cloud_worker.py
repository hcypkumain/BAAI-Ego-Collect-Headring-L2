"""One upload or cleanup process. Credentials arrive only through stdin."""
import base64,contextlib,hashlib,io,json,os,shutil,signal,sys,time
from pathlib import Path
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))
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


def _verify_qualified_source(job,source):
    """Recheck source qualification and enqueue token under the session lease."""
    if source.is_symlink() or not source.is_dir():raise CloudError('上传源目录无效')
    recording=job.get('recording') if isinstance(job.get('recording'),dict) else {}
    if recording.get('qualified') is not True:raise CloudError('上传队列缺少完整质检资格快照')
    meta_path=source/'meta.yaml';summary_path=source/'capture_summary.json'
    selection_path=source/'device_selection.json';report_path=source/'qc_report.json'
    try:
        raw_meta=meta_path.read_bytes();meta=yaml.safe_load(raw_meta.decode('utf-8')) or {}
        summary_raw=summary_path.read_bytes();summary=json.loads(summary_raw.decode('utf-8'),parse_constant=lambda v:(_ for _ in ()).throw(ValueError('nonfinite '+v)))
        selection=json.loads(selection_path.read_text(encoding='utf-8'),parse_constant=lambda v:(_ for _ in ()).throw(ValueError('nonfinite '+v)))
        report=json.loads(report_path.read_text(encoding='utf-8'),parse_constant=lambda v:(_ for _ in ()).throw(ValueError('nonfinite '+v)))
    except (OSError,ValueError,TypeError,UnicodeError,yaml.YAMLError) as exc:
        raise CloudError('本地完整质检证据缺失或无效，请刷新批次：'+str(exc)[:180])
    from required_device_contract import resolve_selection,qualification_errors
    from capture_qc_receipt import verify as verify_qc_receipt
    if not isinstance(meta,dict) or not isinstance(summary,dict) or not isinstance(selection,dict) or not isinstance(report,dict):
        raise CloudError('本地完整质检证据结构无效，请刷新批次')
    contract,contract_errors,legacy=resolve_selection(selection.get('device_config'),selection)
    if contract_errors:raise CloudError('本地必需设备合同无效：'+'；'.join(contract_errors)[:180])
    if meta.get('status')!='success' or summary.get('status')!='success' or summary.get('errors'):
        raise CloudError('本地采集或完整质检未成功，请刷新批次')
    errors=qualification_errors(source,summary,contract,report,legacy=legacy)
    if errors:raise CloudError('本地完整质检不再合格：'+'；'.join(errors[:8]))
    receipt_errors=verify_qc_receipt(source)
    if receipt_errors:raise CloudError('本机完整质检回执已失效：'+'；'.join(receipt_errors[:8]))
    reference=recording.get('reference_path')
    pi=meta.get('pi') if isinstance(meta.get('pi'),dict) else {}
    ref_selection=meta.get('reference_selection') if isinstance(meta.get('reference_selection'),dict) else pi.get('reference_selection')
    asset=ref_selection.get('asset') if isinstance(ref_selection,dict) else None
    frozen=(asset.get('relative_path') if isinstance(asset,dict) else None) or meta.get('reference_image') or pi.get('reference_image')
    if isinstance(frozen,str) and not frozen.startswith('configs/environments/'):
        frozen='configs/environments/'+frozen
    if not isinstance(reference,str) or not reference or frozen!=reference:
        raise CloudError('上传任务参考图身份与本段冻结身份不一致')
    try:
        from recording_store import capture_evidence_token
        signature=source.stat()
        token=hashlib.sha256(str((signature.st_dev,signature.st_ino)).encode()+
            capture_evidence_token(source,raw_meta,summary_raw).encode()).hexdigest()
    except (OSError,ValueError,TypeError,RecursionError) as exc:
        raise CloudError('无法重新核对本地批次内容：'+str(exc)[:180])
    if token!=job.get('source_token'):raise CloudError('本地批次在排队后已变化，请刷新列表后重新选择')

def upload(payload):
    base=Path(payload['base']);job=payload['job'];source=Path(job['source']);directory=Path(payload['directory'])
    if source.resolve().parent!=Path(job['allowed_root']).resolve() or source.name!=job['session_id']:raise CloudError('上传目录越界')
    with session_lock(base,job['session_id']):
        _verify_qualified_source(job,source)
        emit(state='checking',phase='校验本地文件')
        manifest_path=directory/'manifest.json';manifest=read_json(manifest_path)
        if manifest is None:
            manifest=source_manifest(source);atomic(manifest_path,manifest)
        else:check_source(source,manifest)
        emit(total_files=manifest['total_files'],total_bytes=manifest['total_bytes'],completed_files=0,completed_bytes=0)
        from standard_bridge import prepare
        package,package_manifest,conversion=prepare(job,directory,manifest,emit)
        fingerprint=hashlib.sha256(payload['credentials']['ak'].encode()).hexdigest()
        attempt=read_json(directory/'platform.json',{})
        if attempt and attempt.get('account')!=fingerprint:raise CloudError('本批次已绑定另一组平台凭据，请使用原上传账号重试')
        client,vendor,config=authenticate(payload['credentials'])
        projects=client.get_project_list() or []
        matches=[row for row in projects if str(row.get('projectName','')).strip().casefold()==job['settings']['project_name'].strip().casefold()]
        if len(matches)!=1:raise CloudError('找不到唯一匹配的授权项目，请核对项目名称')
        client.project_id=matches[0]['projectId'];client.data_format='LerobotDataset3.1'
        if not client.get_eai_task(job['settings']['task_id'] if job['settings']['task_id'] is not None else -99):raise CloudError('平台任务 ID 无效或无权限')
        bucket=client.get_connection(False).get_bucket(config.BUCKET_NAME)
        prefix=config.UPLOAD_TARGET+'/'+job['remote_path']+'/'
        remote=list(bucket.list(prefix=prefix,delimiter=''))
        if not attempt:
            if remote:raise CloudError('目标目录已有数据，本次未覆盖；请联系维护人员核对')
            data=dict(eai_task_id=client.eai_task_id,task_name=job['remote_path'],source_path=str(source),target_root_path=job['remote_path'],
                target_full_path=job['remote_path'],total_file_count=package_manifest['total_files'],total_size_bytes=package_manifest['total_bytes'],project_id=client.project_id)
            if not client.beigin_upload_eai_task(data) or not client.eai_upload_task_id:raise CloudError('平台尚未确认创建上传任务，未开始传输')
            attempt=dict(account=fingerprint,upload_task_id=client.eai_upload_task_id,project_id=client.project_id,manifest_digest=manifest['digest'],package_version='pi-mf-v3.1-2',package_digest=package_manifest['digest'])
            atomic(directory/'platform.json',attempt)
        else:
            if attempt['manifest_digest']!=manifest['digest'] or attempt['project_id']!=client.project_id:raise CloudError('原上传任务与当前文件或项目不一致')
            client.eai_upload_task_id=attempt['upload_task_id']
        expected={prefix+row['path']:row['bytes'] for row in package_manifest['files']}
        remote_map={row.name:int(row.size) for row in remote}
        if set(remote_map)-set(expected):raise CloudError('目标目录存在本清单以外的文件，已停止以避免混入其他批次')
        completed=0;done_bytes=0;last_platform=0
        emit(state='uploading',phase='上传标准数据包',total_files=package_manifest['total_files'],total_bytes=package_manifest['total_bytes'],completed_files=0,completed_bytes=0,platform_task_id=client.eai_upload_task_id)
        # A previous worker was interrupted: identical manifest and account are
        # required before the vendor's multipart continuation can be reused.
        for row in package_manifest['files']:
            key=prefix+row['path'];path=package/row['path']
            emit(current_file=row['path'],completed_files=completed,completed_bytes=done_bytes)
            if remote_map.get(key)!=row['bytes']:
                if row['bytes']==0:upload_empty(client,config,key)
                else:
                    adapter=Progress(row['bytes'],lambda n:emit(completed_bytes=done_bytes+n))
                    returned=client.upload_file(str(path),job['remote_path'],base_dir=str(package),skip_dir_check=True,show_progress=False,pbar=adapter)
                    if not isinstance(returned,tuple) or len(returned)!=2 or returned[1]!=key:raise CloudError('上传器未确认文件成功，已停止并保留本地数据')
            completed+=1;done_bytes+=row['bytes']
            emit(completed_files=completed,completed_bytes=done_bytes)
            if time.monotonic()-last_platform>10:
                client.update_upload_eai_task_progress(dict(upload_task_id=client.eai_upload_task_id,success_file_count=completed,failed_file_count=0,data_format=client.data_format));last_platform=time.monotonic()
        emit(state='verifying',phase='核验云端文件与本地清单',current_file='')
        client._refresh_sts_if_needed();bucket=client.get_connection(False).get_bucket(config.BUCKET_NAME)
        verified=verify_remote(package_manifest,prefix,bucket.list(prefix=prefix,delimiter=''))
        check_source(package,package_manifest)
        check_source(source,manifest)
        if not client.update_upload_eai_task_progress(dict(upload_task_id=client.eai_upload_task_id,success_file_count=completed,failed_file_count=0,data_format=client.data_format)):
            raise CloudError('文件已传输，但平台尚未确认最终进度；重试核验后才可清理')
        if not client.complete_upload_eai_task('SUCCESS'):raise CloudError('文件已核验，但平台尚未确认上传完成；暂不可清理')
        receipt=dict(version=1,status='success',session_id=job['session_id'],job_id=job['id'],remote_path=job['remote_path'],
                     manifest_digest=manifest['digest'],package_digest=package_manifest['digest'],package_path=str(package),package_version='pi-mf-v3.1-2',data_format=client.data_format,platform_task_id=client.eai_upload_task_id,project_id=client.project_id,
                     platform_completion_acknowledged=True,verification=verified,account=fingerprint,completed_at=time.time())
        atomic(directory/'receipt.json',receipt)
        emit(state='success',phase='已上传并核验，可清理本地数据',completed_at=receipt['completed_at'],receipt=receipt,
             completed_files=package_manifest['total_files'],completed_bytes=package_manifest['total_bytes'],current_file='')

def cleanup(payload):
    job=payload['job'];source=Path(job['source']);directory=Path(payload['directory']);base=Path(payload['base'])
    with session_lock(base,job['session_id']):
        manifest=read_json(directory/'manifest.json');receipt=read_json(directory/'receipt.json')
        if not receipt or receipt.get('status')!='success' or receipt.get('manifest_digest')!=manifest.get('digest') or not receipt.get('platform_completion_acknowledged'):
            raise CloudError('没有完整云端成功回执，禁止删除本地数据')
        if source.resolve().parent!=Path(job['allowed_root']).resolve() or source.name!=job['session_id']:raise CloudError('清理目录越界')
        from standard_bridge import check_cleanup,cleanup_cache
        check_cleanup(job,directory,receipt)
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
        cleanup_cache(job,directory,receipt)
        intent.update(phase='complete',completed_at=time.time());atomic(intent_path,intent)
        # The reference ledger and receipt are intentionally retained. This is
        # storage cleanup, not invalidating/deleting a failed recording.
        emit(state='success',phase='云端已上传，本地副本已清理',local_deleted=True,cleaned_at=time.time())

def cancel_group(signum,frame):
    signal.signal(signum,signal.SIG_IGN)
    if os.getpgrp()==os.getpid():os.killpg(os.getpgrp(),signal.SIGTERM)
    raise SystemExit(128+signum)

def main():
    signal.signal(signal.SIGTERM,cancel_group)
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
