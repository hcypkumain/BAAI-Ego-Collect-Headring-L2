import hashlib,importlib.util,json,os
from pathlib import Path
import sys,tempfile,types,unittest,uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
from raw_handoff import signed,sha_value,RAW_PURPOSE,RESULT_PURPOSE
from required_device_contract import build_contract
from capture_qc_receipt import input_signature,portable_input_signature

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
        config={'collection_mode':'emg',
            'cameras':{name:{'camd':'/dev/camd-'+name} for name in ('left','right','bleft','bright')},
            'wrist_cameras':{name:{'device':'/dev/'+name} for name in ('cam_left_wrist','cam_right_wrist')},
            'emg':{name:{'port':'/dev/'+name} for name in ('emg_left','emg_right')},
            'board_imu':{'device':'/dev/board-imu'}}
        contract=build_contract(config,mode='emg')
        selection={'selected':contract['selected_devices'],'device_config':config,'collection_mode':'emg',
                   'required_device_contract':contract}
        (self.source/'device_selection.json').write_text(json.dumps(selection))
        summary={'status':'pending_qc','capture_status':'success','qc_state':'pending',
                 'qc_level':'PENDING','errors':[],
                 'start_unix_ns':1_800_000_000_000_000_000,
                 'stop_unix_ns':1_800_000_010_000_000_000}
        for name in contract['required_devices']:
            directory=self.source/name;directory.mkdir()
            if name.startswith('cam_'):
                (directory/'frames.mp4').write_bytes(b'fixture-video-'+name.encode())
                (directory/'timestamps.csv').write_text('timestamp\n1\n')
                summary[name]={'frames':1}
            elif name.startswith('emg_'):
                (directory/(name+'.npz')).write_bytes(b'fixture-array-'+name.encode())
                summary[name]={'emg_frames':1}
            else:
                (directory/'raw.csv').write_text('timestamp\n1\n')
                summary[name]={'frames':1}
        native={'status':'success','fixture':True}
        (self.source/'native_capture_summary.json').write_text(json.dumps(native))
        reference_sha='a'*64
        (self.source/'meta.json').write_text(json.dumps({'reference_sha256':reference_sha,'collection_mode':'emg'}))
        (self.source/'meta.yaml').write_text('status: pending_qc\n')
        (self.source/'capture_summary.json').write_text(json.dumps(summary))
        (self.source/'start_gate.json').write_text(json.dumps({'start_unix_ns':summary['start_unix_ns'],
            'start_monotonic_ns':1_000_000_000}))
        (self.source/'stop_gate.json').write_text(json.dumps({'stop_unix_ns':summary['stop_unix_ns'],
            'stop_monotonic_ns':11_000_000_000,'recording_active':False}))
        (self.source/'status.json').write_text(json.dumps({'state':'pending_qc','recording_active':False}))
        pending={'version':1,'request_id':self.request_id,'session_id':self.sid,'state':'pending',
            'native_sha256':hashlib.sha256((self.source/'native_capture_summary.json').read_bytes()).hexdigest(),
            'reference_sha256':reference_sha,'legacy_source':False}
        pending['input_signature']=portable_input_signature(self.source,contract,require_marker=False)
        pending['local_input_signature']=input_signature(self.source,contract,require_marker=False)
        (self.source/'qc_pending.json').write_text(json.dumps(pending))
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
            host_verified=True,host_fsynced=True,host_path='/host/fixture',host_storage_uuid='HOST-TEST',
            qc_protocol_version=2,qc_qualification_protocol='required-device-contract-v1',
            input_signature=pending['input_signature'],local_input_signature=pending['local_input_signature'],
            input_signature_kind='portable-content-sha256-v1',reference_sha256=reference_sha,
            required_device_contract_version=contract['version']),self.key)
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
            passed=False,input_signature=self.receipt['input_signature'],
            local_input_signature=self.receipt['local_input_signature'],
            reference_sha256=self.receipt['reference_sha256'],checked_input_signature='b'*64,
            artifacts={'qc_report.json':hashlib.sha256(report.read_bytes()).hexdigest()})
        (resultdir/'result.json').write_text(json.dumps(result))
        envelope=signed(dict(version=1,purpose=RESULT_PURPOSE,device_id='cp_0020',server_id='fixture-server',
            session_id=self.sid,handoff_receipt_sha256=sha_value(self.receipt),
            result_sha256=hashlib.sha256((resultdir/'result.json').read_bytes()).hexdigest(),server_path='/host/results',
            qc_protocol_version=2,qc_qualification_protocol='required-device-contract-v1',
            input_signature=self.receipt['input_signature'],local_input_signature=self.receipt['local_input_signature'],
            checked_input_signature='b'*64,reference_sha256=self.receipt['reference_sha256'],
            passed=False,qualification_receipt_sha256=None),self.key)
        ack=self.agent.apply_handoff_result({'receipt':envelope});self.assertTrue(ack['retake_required'])
        self.assertFalse(self.source.exists());self.assertEqual(ack,self.agent.apply_handoff_result({'receipt':envelope}))
    def test_legacy_cleanup_rejects_raw_receipt_purpose(self):
        with self.assertRaises(ValueError):self.agent.cleanup({'receipt':self.receipt})
        self.assertTrue(self.source.exists())

if __name__=='__main__':unittest.main(verbosity=2)
