import contextlib,copy,hashlib,io,json,os,tempfile,types,unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control')]
from unittest.mock import Mock,patch
import cloud_common as common
import cloud_worker as worker
import cloud_uploads as queue
import standard_bridge

class Store:
    def __init__(self,base):
        self.base=base;self.root=base/'volume/data/session-night';self.root.mkdir(parents=True)
        self.session='L2-001-2026-11-02-00-53-37';self.source=self.root/self.session;self.source.mkdir()
        (self.source/'meta.yaml').write_text('test: true\n');(self.source/'video.bin').write_bytes(b'original video data');(self.source/'empty').touch()
        # This fake Store is the protocol-test boundary: full-QC qualification
        # is independently exercised by RecordingStore fault-injection tests.
        self.meta=dict(id=self.session,token='unchanged',retained=True,qualified=True,
            qualification_error='',qc_state='completed',reference_name='test',qc_level='WARN',day='2026-11-02',
            created_unix=1793552017,duration=14,reference_state='collected')
    def roots(self):return self.root,self.base/'volume'
    def path(self,identifier):
        if identifier!=self.session or not self.source.is_dir():raise ValueError('missing')
        return self.source
    def metadata(self,path):return dict(self.meta)
    def entries(self,day):return ([dict(self.meta)] if self.source.is_dir() else []),'2026-11-02'

class Client:
    def __init__(self):self.eai_upload_task_id=None;self.objects={};self.begin_ok=True;self.complete_ok=True;self.progress_ok=True;self.fail_file=None;self.uploaded=[];self.completions=[];self.starts=0;self.empty_requests=[];self.empty_status=200;self.empty_store=True
    def get_project_list(self):return [dict(projectName='EGOL2',projectId=1)]
    def get_eai_task(self,value):self.eai_task_id=value;return True
    def get_connection(self,*args):return self
    def get_bucket(self,*args):return self
    def make_request(self,method,bucket,key,data,headers):
        if Path(key).name==self.fail_file:raise common.CloudError('injected file failure')
        self.empty_requests.append((method,key,data,headers))
        if self.empty_store:self.objects[key]=0
        return types.SimpleNamespace(status=self.empty_status,read=lambda:b'')
    def get_key(self,key):raise AssertionError('Object HEAD may be denied to upload-only accounts')
    def list(self,prefix,delimiter):return [types.SimpleNamespace(name=name,size=size) for name,size in self.objects.items() if name.startswith(prefix)]
    def beigin_upload_eai_task(self,data):self.starts+=1;self.eai_upload_task_id='platform-123';return self.begin_ok
    def upload_file(self,path,target_directory,base_dir,skip_dir_check,show_progress,pbar):
        if Path(path).name==self.fail_file:raise common.CloudError('injected file failure')
        assert skip_dir_check and show_progress is False
        key='data/'+target_directory+'/'+Path(path).relative_to(base_dir).as_posix();size=Path(path).stat().st_size
        self.objects[key]=size;self.uploaded.append(key);pbar.update(size);return size,key
    def update_upload_eai_task_progress(self,data):return self.progress_ok
    def complete_upload_eai_task(self,state):self.completions.append(state);return self.complete_ok
    def _refresh_sts_if_needed(self):pass

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.store=Store(self.base);self.display=dict(state='idle',privacy={'required':False},worker={})
        self.manager=queue.UploadManager(self.base,self.store,lambda:self.display,autostart=False);self.addCleanup(self.manager.close)
        self.manager.set_credentials(dict(ak='example-access',sk='example-secret'))
        self.manager.enqueue(dict(selection=[dict(session_id=self.store.session,token='unchanged')],settings=dict(queue.DEFAULTS)))
        self.job=self.manager.state['jobs'][0];self.directory=self.manager.root/'jobs'/self.job['id'];self.directory.mkdir(parents=True)
        self.payload=dict(base=str(self.base),directory=str(self.directory),job=copy.deepcopy(self.job),credentials=dict(ak='example-access',sk='example-secret'))
        self.client=Client();self.vendor=types.SimpleNamespace(detect_data_format=lambda source:'OTHER');self.config=types.SimpleNamespace(UPLOAD_TARGET='data',BUCKET_NAME='bucket')
        self.patch=patch.object(worker,'authenticate',return_value=(self.client,self.vendor,self.config));self.patch.start();self.addCleanup(self.patch.stop)
        def verify_prequalified_protocol_source(job,source):
            if job.get('recording',{}).get('qualified') is not True:
                raise common.CloudError('protocol fixture must start with an already qualified source')
        self.qualification=patch.object(worker,'_verify_qualified_source',side_effect=verify_prequalified_protocol_source)
        self.qualification.start();self.addCleanup(self.qualification.stop)
        # This suite covers queue transfer/receipt semantics; the isolated
        # standard exporter and mounted-volume guard have separate coverage.
        self.prepare=patch.object(standard_bridge,'prepare',side_effect=lambda job,directory,manifest,emit:(self.store.source,manifest,{}))
        self.prepare.start();self.addCleanup(self.prepare.stop)
        self.package_cleanup=patch.multiple(standard_bridge,check_cleanup=Mock(return_value=None),cleanup_cache=Mock())
        self.package_cleanup.start();self.addCleanup(self.package_cleanup.stop)
        self.events=[];self.emit=patch.object(worker,'emit',side_effect=lambda **event:self.events.append(event));self.emit.start();self.addCleanup(self.emit.stop)
    def run_upload(self):worker.upload(self.payload)
    def test_credentials_encrypted_and_masked_not_sent_in_status(self):
        encrypted=(self.manager.root/'credentials.enc').read_bytes();self.assertNotIn(b'example-secret',encrypted)
        self.assertNotIn('example-access',json.dumps(self.manager.status('all')));self.assertEqual((self.manager.root/'vault.key').stat().st_mode&0o777,0o600)
    def test_begin_failure_does_not_transfer_or_publish_success(self):
        self.client.begin_ok=False
        with self.assertRaises(common.CloudError):self.run_upload()
        self.assertEqual(self.client.uploaded,[]);self.assertFalse((self.directory/'receipt.json').exists())
    def test_empty_objects_use_literal_body_and_require_remote_acknowledgement(self):
        self.run_upload();self.assertEqual(len(self.client.empty_requests),1)
        method,key,body,headers=self.client.empty_requests[0]
        self.assertEqual(method,'PUT');self.assertEqual(body,b'');self.assertEqual(headers['Content-Length'],'0');self.assertNotIn('Transfer-Encoding',headers)
    def test_empty_put_http_failure_does_not_publish_success(self):
        self.client.empty_status=500
        with self.assertRaises(common.CloudError):self.run_upload()
        self.assertFalse((self.directory/'receipt.json').exists())
    def test_empty_put_without_matching_remote_object_does_not_publish_success(self):
        self.client.empty_store=False
        with self.assertRaises(common.CloudError):self.run_upload()
        self.assertFalse((self.directory/'receipt.json').exists())
    def test_zero_byte_file_failure_never_counts_as_complete(self):
        self.client.fail_file='empty'
        with self.assertRaises(common.CloudError):self.run_upload()
        self.assertNotIn('SUCCESS',self.client.completions);self.assertFalse((self.directory/'receipt.json').exists())
    def test_platform_completion_failure_blocks_cleanup(self):
        self.client.complete_ok=False
        with self.assertRaises(common.CloudError):self.run_upload()
        self.job['state']='success';self.assertFalse(self.manager.can_cleanup(self.job))
    def test_full_success_receipt_and_source_change_revoke_cleanup(self):
        self.run_upload();self.job['state']='success';self.assertTrue(self.manager.can_cleanup(self.job))
        receipt=common.read_json(self.directory/'receipt.json');self.assertTrue(receipt['platform_completion_acknowledged'])
        (self.store.source/'video.bin').write_bytes(b'changed');self.assertFalse(self.manager.can_cleanup(self.job,fresh=True))
        with self.assertRaises(common.CloudError):worker.cleanup(self.payload)
        self.assertTrue(self.store.source.exists())
    def test_failed_attempt_resumes_same_manifest_without_duplicate_files(self):
        self.client.complete_ok=False
        with self.assertRaises(common.CloudError):self.run_upload()
        transferred=len(self.client.uploaded);self.client.complete_ok=True;self.run_upload()
        self.assertEqual(len(self.client.uploaded),transferred);self.assertEqual(self.client.starts,1)
    def test_other_account_cannot_resume_existing_platform_task(self):
        self.client.complete_ok=False
        with self.assertRaises(common.CloudError):self.run_upload()
        self.payload['credentials']['ak']='other'
        with self.assertRaises(common.CloudError):self.run_upload()
    def test_cleanup_deletes_only_successful_source_preserves_receipt_and_reference(self):
        self.run_upload();self.job['state']='success';worker.cleanup(self.payload)
        self.assertFalse(self.store.source.exists());self.assertTrue((self.directory/'receipt.json').exists());self.assertEqual(self.store.meta['reference_state'],'collected')
        self.assertEqual(common.read_json(self.directory/'cleanup.json')['phase'],'complete')
    def test_exact_remote_set_and_file_sizes_required(self):
        manifest=common.source_manifest(self.store.source)
        with self.assertRaises(common.CloudError):common.verify_remote(manifest,'data/',[types.SimpleNamespace(name='data/meta.yaml',size=11)])
    def test_queued_data_mutations_are_checked_before_worker(self):
        self.store.meta['retained']=False;self.manager._launch(self.job);self.assertEqual(self.job['state'],'failed')
    def test_enqueue_refuses_unqualified_source_at_the_live_queue_gate(self):
        self.store.meta['qualified']=False
        request=dict(selection=[dict(session_id=self.store.session,token='unchanged')],settings=dict(queue.DEFAULTS))
        count=len(self.manager.state['jobs'])
        with patch.object(queue.subprocess,'Popen') as spawn, self.assertRaises(common.CloudError):
            self.manager.enqueue(request)
        self.assertEqual(len(self.manager.state['jobs']),count);spawn.assert_not_called()
    def test_active_capture_blocks_local_cleanup(self):
        self.run_upload();self.job['state']='success';self.display['state']='recording'
        with self.assertRaises(common.CloudError):self.manager.cleanup(dict(job_ids=[self.job['id']],confirmation='DELETE_UPLOADED_LOCAL'))
    def test_upload_and_mutation_share_exclusive_session_lease(self):
        with common.session_lock(self.base,self.store.session):
            with self.assertRaises(common.CloudError):self.run_upload()
    def test_recovery_requires_receipt_not_stale_success_flag(self):
        self.job['state']='uploading';self.manager._save()
        other=queue.UploadManager(self.base,self.store,lambda:self.display,autostart=False)
        try:self.assertEqual(other.state['jobs'][0]['state'],'interrupted')
        finally:other.close()
    def test_collection_start_pauses_the_transfer_process(self):
        process=Mock();process.poll.return_value=None
        self.manager.active=self.job;self.manager.process=process;self.manager.pause_reason=None;self.manager.last_save=0
        self.job['state']='uploading';self.display['state']='preparing';self.manager._poll()
        process.terminate.assert_called_once();self.assertIn('采集优先',self.manager.pause_reason)
    def test_success_receipt_survives_queue_service_restart(self):
        self.run_upload();self.job['state']='uploading';self.manager._save()
        other=queue.UploadManager(self.base,self.store,lambda:self.display,autostart=False)
        try:self.assertEqual(other.state['jobs'][0]['state'],'success');self.assertTrue(other.can_cleanup(other.state['jobs'][0]))
        finally:other.close()

if __name__=='__main__':unittest.main(verbosity=2)
