import hashlib,importlib.util,json,os
from pathlib import Path
import sys,tempfile,types,unittest,uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
from raw_handoff import signed,sha_value,RAW_PURPOSE,RESULT_PURPOSE

@unittest.skipIf(os.name=='nt','fcntl and Pi filesystem protocol are validated on Linux')
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.base=base/'project/orangepi';self.base.mkdir(parents=True)
        self.volume=base/'volume';self.root=self.volume/'data/session-night';self.sid='L2-020-fixture'
        self.source=self.root/self.sid;self.source.mkdir(parents=True)
        (self.base/'private').mkdir();self.key=b'synthetic-custody-test-key'
        (self.base/'private/server_qc_hmac').write_bytes(self.key)
        self.request_id=uuid.uuid4().hex
        for name,value in {'start_gate.json':{'start_unix_ns':1},'stop_gate.json':{'recording_active':False},
             'status.json':{'state':'pending_qc','recording_active':False},'meta.json':{},
             'native_capture_summary.json':{'status':'success','fixture':True},
             'qc_pending.json':{'request_id':self.request_id}}.items():
            (self.source/name).write_text(json.dumps(value))
        (self.source/'raw.bin').write_bytes(b'actual fixture bytes, not a real capture')
        project=types.ModuleType('project_paths');project.BASE=self.base;project.PROJECT=self.base.parent
        ledger=types.ModuleType('ledger');storage=types.ModuleType('storage_paths')
        storage.config=lambda:{'expected_uuid':'TEST'}
        common=ROOT/'orangepi/src/orangepi_collect/control'
        spec=importlib.util.spec_from_file_location('fixture_pi_agent',ROOT/'services/headring-qc/pi_agent.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'project_paths':project,'ledger':ledger,'storage_paths':storage}), \
             patch.dict(os.environ,{'HEADRING_PI_BASE':str(self.base)}),patch.object(sys,'path',[str(common)]+sys.path):
            spec.loader.exec_module(module)
        self.agent=module;module.identity=lambda:'cp_0020';module.roots=lambda:(self.root,self.volume)
        module.idle=lambda:{'state':'device_confirm','session':None};module.RESULTS=base/'results'
        def finalized(path):
            module.idle();state=json.loads((path/'status.json').read_text())
            if state['state'] not in module.TERMINAL or state.get('recording_active'):raise ValueError('not finalized')
            return state
        module.finalized=finalized
        (module.STATE/'server_identity.json').write_text(json.dumps({'server_id':'fixture-server'}))
        manifest=module.manifest(self.sid)
        self.receipt=signed(dict(version=1,purpose=RAW_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,transaction_id=uuid.uuid4().hex,manifest=manifest,qc_request_id=self.request_id,
            host_verified=True,host_fsynced=True,host_path='/host/fixture',host_storage_uuid='HOST-TEST'),self.key)
    def handoff(self):return self.agent.raw_handoff({'receipt':self.receipt})
    def test_raw_handoff_deletes_only_complete_named_fixture_before_qc(self):
        self.assertFalse((self.source/'qc_applied.json').exists())
        other=self.root/'L2-020-other';other.mkdir();(other/'keep').write_bytes(b'untouched')
        ack=self.handoff();self.assertEqual(ack['state'],'deleted');self.assertFalse(self.source.exists())
        self.assertEqual((other/'keep').read_bytes(),b'untouched')
        self.assertEqual(ack['receipt_sha256'],sha_value(self.receipt))
    def test_changed_source_retained(self):
        (self.source/'raw.bin').write_bytes(b'changed after manifest')
        with self.assertRaises(ValueError):self.handoff()
        self.assertTrue(self.source.exists())
    def test_extra_or_missing_file_retained(self):
        (self.source/'foreign-file').write_bytes(b'do not delete')
        with self.assertRaises(ValueError):self.handoff()
        self.assertTrue(self.source.exists())
    def test_bad_signature_and_busy_state_retained(self):
        receipt=dict(self.receipt,signature='0'*64)
        with self.assertRaises(ValueError):self.agent.raw_handoff({'receipt':receipt})
        with patch.object(self.agent,'idle',side_effect=RuntimeError('PI_BUSY')):
            with self.assertRaises(RuntimeError):self.handoff()
        self.assertTrue(self.source.exists())
    def test_ack_replay_never_deletes_reused_original_name(self):
        first=self.handoff();self.source.mkdir();(self.source/'new-user-work').write_bytes(b'keep')
        second=self.handoff();self.assertEqual(first,second)
        self.assertEqual((self.source/'new-user-work').read_bytes(),b'keep')
    def test_completed_handoff_query_survives_profile_switch(self):
        self.handoff();self.agent.roots=lambda:(_ for _ in ()).throw(ValueError('active mode changed'))
        result=self.agent.handoff_status({'session_id':self.sid,'handoff_receipt_sha256':sha_value(self.receipt)})
        self.assertEqual(result['state'],'deleted')
    def test_partial_recursive_delete_recovers_without_requiring_missing_files(self):
        real=self.agent.shutil.rmtree
        def interrupted(target):
            next(p for p in Path(target).rglob('*') if p.is_file()).unlink()
            raise OSError('simulated process interruption')
        with patch.object(self.agent.shutil,'rmtree',side_effect=interrupted):
            with self.assertRaises(OSError):self.handoff()
        record=json.loads(self.agent._handoff_record(self.sid).read_text());self.assertEqual(record['state'],'deleting_files')
        ack=self.handoff();self.assertEqual(ack['state'],'deleted')
        self.assertFalse(Path(record['trash_path']).exists())
    def test_post_handoff_qc_does_not_need_deleted_raw_directory(self):
        self.handoff()
        ready=self.agent.prepare_handoff_results({'session_id':self.sid,'handoff_receipt_sha256':sha_value(self.receipt)})
        resultdir=Path(ready['result_directory']);(resultdir/'artifacts').mkdir()
        report=resultdir/'artifacts/qc_report.json';report.write_text(json.dumps({'level':'ERROR','streams':{}}))
        result=dict(version=1,session_id=self.sid,request_id=self.request_id,
            native_sha256=self.receipt['manifest']['native_sha256'],level='ERROR',raw_level='ERROR',
            artifacts={'qc_report.json':hashlib.sha256(report.read_bytes()).hexdigest()})
        (resultdir/'result.json').write_text(json.dumps(result))
        envelope=signed(dict(version=1,purpose=RESULT_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,handoff_receipt_sha256=sha_value(self.receipt),
            result_sha256=hashlib.sha256((resultdir/'result.json').read_bytes()).hexdigest(),server_path='/host/results'),self.key)
        ack=self.agent.apply_handoff_result({'receipt':envelope});self.assertTrue(ack['retake_required'])
        self.assertFalse(self.source.exists());self.assertEqual(ack,self.agent.apply_handoff_result({'receipt':envelope}))
    def test_legacy_cleanup_rejects_raw_receipt_purpose(self):
        with self.assertRaises(ValueError):self.agent.cleanup({'receipt':self.receipt})
        self.assertTrue(self.source.exists())

if __name__=='__main__':unittest.main(verbosity=2)
