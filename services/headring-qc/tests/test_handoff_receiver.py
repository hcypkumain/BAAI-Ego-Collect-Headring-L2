import hashlib,json,os
from pathlib import Path
import sys,tempfile,threading,types,unittest,uuid
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'services/headring-qc'));sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
from handoff_receiver import HandoffReceiver
from receiver_state import Conflict
from raw_handoff import sha_value

@unittest.skipIf(os.name=='nt','Linux copy/fsync receiver tests')
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        self.app=root/'app';self.app.mkdir();self.data=root/'data';self.data.mkdir();self.source=root/'pi-source/L2-020-fixture';self.source.mkdir(parents=True)
        for name,value in {'native_capture_summary.json':{'status':'success','fixture':True},'capture_summary.json':{'status':'pending_qc'},'meta.json':{},'status.json':{'state':'pending_qc'}}.items():
            (self.source/name).write_text(json.dumps(value))
        (self.source/'meta.yaml').write_text('fixture: true\n');(self.source/'raw.bin').write_bytes(b'fixture raw data')
        self.key=b'synthetic-test-key';(self.app/'secrets').mkdir();(self.app/'secrets/cp_0020').write_bytes(self.key)
        def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
        def verify(path,manifest,durable=False):
            if self.verify_hook:self.verify_hook(Path(path))
            actual={p.relative_to(path).as_posix() for p in Path(path).rglob('*') if p.is_file()}
            if actual!={r['path'] for r in manifest['files']}:raise ValueError('file set mismatch')
            for row in manifest['files']:
                p=Path(path)/row['path']
                if digest(p)!=row['sha256'] or p.stat().st_size!=row['bytes']:raise ValueError('SHA mismatch')
            return {'verified':True,'durable':durable}
        self.verify_hook=None
        legacy=types.SimpleNamespace(ROOT=self.app,DATA=self.data,STOP=threading.Event(),CONFIG={'devices':[{'id':'cp_0020'}],
            'server_id':'fixture-server','storage_uuid':'TEST','qc_workers':1},require_storage=lambda cfg:None,hash_file=digest,
            verify_tree=verify,sync_dir=lambda path:None,atomic=lambda path,value:self.write_json(path,value))
        self.receiver=HandoffReceiver(legacy);self.receiver.store.set_control(True,0,uuid.uuid4().hex);self.rpc_calls=[]
        def rpc(device,action,**values):
            self.rpc_calls.append(action)
            if action=='status':return {'device_id':device,'state':'device_confirm','session':None,'worker':{}}
            if action=='manifest':
                m=self.receiver._manifest(self.source);m.update(device_id=device,session_id=self.source.name,root=str(self.source.parent));return m
            if action=='handoff_status':return {'device_id':device,'state':'missing'}
            if action=='raw_handoff':
                import shutil
                shutil.rmtree(self.source);return {'state':'deleted','receipt_sha256':sha_value(values['receipt'])}
            raise AssertionError('Unexpected Pi RPC '+action)
        self.receiver.rpc=rpc
        def transfer(job,dest,push=False):
            import shutil
            self.assertFalse(push);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(self.source,dest)
        self.receiver._transfer=transfer
        details=dict(session_id=self.source.name,path=str(self.source),native_sha256=digest(self.source/'native_capture_summary.json'),
            request_id=uuid.uuid4().hex,day='2026-11-08',bytes=100)
        self.id=self.receiver.store.enqueue('cp_0020',details)
    def write_json(self,path,value):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
    def receive(self):
        job=self.receiver.store.claim('receive');self.receiver.receive(job);return self.receiver.store.job(self.id)
    def fake_qc(self,job,work):
        resultdir=self.data/'results'/job['device']/job['session'];artifact=resultdir/'artifacts/qc_report.json'
        self.write_json(artifact,{'level':'WARN','streams':{}})
        result=dict(version=1,session_id=job['session'],request_id=job['details']['request_id'],native_sha256=job['details']['native_sha256'],
            level='WARN',raw_level='ERROR',elapsed_seconds=1,metadata={},artifacts={'qc_report.json':hashlib.sha256(artifact.read_bytes()).hexdigest()})
        self.write_json(resultdir/'result.json',result);return result
    def test_host_committed_receipt_precedes_any_pi_delete(self):
        job=self.receive();self.assertEqual(job['receive_state'],'host_committed')
        self.assertTrue(self.source.exists());self.assertNotIn('raw_handoff',self.rpc_calls)
        self.assertTrue(Path(job['raw_path']).exists());self.assertEqual(job['process_state'],'queued')
    def test_stop_wins_before_delete_authorization(self):
        job=self.receive()
        def stop(path):
            if path==Path(job['raw_path']):
                state=self.receiver.store.control();self.receiver.store.set_control(False,state['revision'],uuid.uuid4().hex)
        self.verify_hook=stop
        with self.assertRaises(Conflict):self.receiver.release(job)
        self.assertTrue(self.source.exists());self.assertNotIn('raw_handoff',self.rpc_calls)
    def test_delete_then_qc_works_with_stopped_receiver_and_offline_pi(self):
        job=self.receive();self.receiver.release(job);self.assertFalse(self.source.exists())
        state=self.receiver.store.control();self.receiver.store.set_control(False,state['revision'],uuid.uuid4().hex)
        self.receiver.rpc=lambda *a,**k:(_ for _ in ()).throw(AssertionError('PC QC must not contact Pi'))
        self.receiver._qc=self.fake_qc;self.receiver.process(self.receiver.store.job(self.id))
        completed=self.receiver.store.job(self.id);self.assertEqual(completed['process_state'],'complete')
        self.assertTrue(Path(completed['raw_path']).exists());self.assertTrue(Path(completed['archive_path']).exists())
        self.assertTrue(completed['retention_hold']);self.assertIsNone(completed['cloud_confirmed_at'])
    def test_committed_archive_recovers_without_repeating_qc(self):
        self.receive();self.receiver._qc=self.fake_qc;self.receiver.process(self.receiver.store.job(self.id))
        self.receiver.store.update(self.id,process_state='retry_wait')
        self.receiver._qc=lambda *a:(_ for _ in ()).throw(AssertionError('QC repeated'))
        self.receiver.process(self.receiver.store.job(self.id));self.assertEqual(self.receiver.store.job(self.id)['process_state'],'complete')

if __name__=='__main__':unittest.main(verbosity=2)
