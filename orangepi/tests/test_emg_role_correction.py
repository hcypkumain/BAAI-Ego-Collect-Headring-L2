import copy,datetime,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import yaml
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"tools/maintenance"))
import emg_role_correction as roles

class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.root=self.base/'data/take';self.root.mkdir(parents=True);self.backup=self.base/'backups'
        start=int(datetime.datetime(2026,11,2,12,tzinfo=roles.TZ).timestamp()*1e9)
        proof={'method':'ordered_insertion','roles':{'emg_left':'receiver-R','emg_right':'receiver-L'},'first_side':'left'}
        config={'emg':{'emg_left':{'port':'receiver-R','label':'左臂肌电'},'emg_right':{'port':'receiver-L','label':'右臂肌电'}},'emg_calibration_status':{'valid':True,'proof':proof}}
        meta={'device_config':config,'reference_image':'keep-image.png','status':'success','retention_state':'kept'}
        (self.root/'meta.json').write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
        (self.root/'meta.yaml').write_text(yaml.safe_dump(dict(meta,pi=meta),allow_unicode=True),encoding='utf-8')
        summary={'status':'success','qc_level':'WARN','start_unix_ns':start,'emg_left':{'port':'receiver-R','emg_frames':3},'emg_right':{'port':'receiver-L','emg_frames':4},'cam_left_wrist':{'frames':12}}
        (self.root/'capture_summary.json').write_text(json.dumps(summary))
        (self.root/'storage_sync.json').write_text('{"completed":true}')
        (self.root/'qc_raw_report.json').write_text(json.dumps({'level':'WARN','streams':{'emg_left':{'observed':3},'emg_right':{'observed':4},'cam_left_wrist':{'observed':12}}}))
        (self.root/'qc_report.json').write_text(json.dumps({'level':'WARN','pi_qc_policy':{},'streams':{'emg_left':{'observed':3},'emg_right':{'observed':4}}}))
        (self.root/'qc_policy.json').write_text('{}')
        for side,n in (('left',3),('right',4)):
            path=self.root/('emg_'+side);path.mkdir()
            np.savez(path/('emg_'+side+'.npz'),emg_data=np.arange(n*3).reshape(n,3),imu_accel=np.arange(n*3).reshape(n,3),emg_timestamps=np.arange(n)+1000)
            (path/('emg_'+side+'.log')).write_text(json.dumps(summary['emg_'+side]))
        for name in ('cam_left_wrist','cam_right_wrist'):
            path=self.root/name;path.mkdir();(path/'frames.mp4').write_bytes(name.encode())
        self.original_proof=copy.deepcopy(proof)

    def test_swap_is_lossless_for_emg_imu_and_wrist_files(self):
        before=roles.signal_manifest(self.root);wrists=roles.wrist_manifest(self.root)
        result=roles.correct_session(self.root,self.backup,'2026-11-02')
        self.assertTrue(result['corrected']);after=roles.signal_manifest(self.root)
        for name,row in before.items():
            if name.endswith('.npz'):self.assertEqual(row,after[roles.flip_text(name)])
        self.assertEqual(wrists,roles.wrist_manifest(self.root))
        with np.load(self.root/'emg_left/emg_left.npz') as z:self.assertEqual(len(z['imu_accel']),4)
        self.assertTrue((self.backup/'take/emg_left/emg_left.npz').exists())
    def test_metadata_qc_keys_and_proof_provenance_are_consistent(self):
        roles.correct_session(self.root,self.backup,'2026-11-02')
        meta=json.loads((self.root/'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['device_config']['emg']['emg_left']['port'],'receiver-L')
        self.assertEqual(meta['device_config']['emg_calibration_status']['proof'],self.original_proof)
        self.assertIn('emg_physical_role_correction',meta['device_config'])
        summary=json.loads((self.root/'capture_summary.json').read_text());self.assertEqual(summary['emg_left']['emg_frames'],4);self.assertEqual(summary['cam_left_wrist']['frames'],12)
        report=json.loads((self.root/'qc_report.json').read_text());self.assertEqual(report['streams']['emg_left']['observed'],4)
        self.assertEqual(report['pi_qc_policy']['raw_report_sha256'],roles.sha(self.root/'qc_raw_report.json'))
        self.assertEqual(yaml.safe_load((self.root/'meta.yaml').read_text(encoding='utf-8'))['retention_state'],'kept')
    def test_second_run_cannot_swap_back(self):
        roles.correct_session(self.root,self.backup,'2026-11-02');first=roles.signal_manifest(self.root)
        self.assertTrue(roles.correct_session(self.root,self.backup,'2026-11-02')['already_corrected']);self.assertEqual(first,roles.signal_manifest(self.root))
    def test_unauthorized_capture_date_is_refused_without_mutation(self):
        before=roles.signal_manifest(self.root)
        with self.assertRaises(ValueError):roles.correct_session(self.root,self.backup,'2026-11-01')
        self.assertEqual(before,roles.signal_manifest(self.root))
    def test_failure_rolls_back_original_streams_and_metadata(self):
        before=roles.signal_manifest(self.root);metadata=(self.root/'meta.json').read_bytes()
        def fail(root):raise OSError('injected disk-sync failure')
        with self.assertRaisesRegex(OSError,'injected disk-sync'):roles.correct_session(self.root,self.backup,'2026-11-02',syncer=fail)
        self.assertEqual(before,roles.signal_manifest(self.root));self.assertEqual(metadata,(self.root/'meta.json').read_bytes())
        self.assertFalse((self.root/'emg_role_correction.json').exists())
        self.assertEqual(json.loads((self.backup/'take/journal.json').read_text())['state'],'rolled_back')
    def test_existing_error_is_not_promoted_by_side_correction(self):
        p=self.root/'capture_summary.json';value=json.loads(p.read_text());value.update(status='error',qc_level='ERROR');p.write_text(json.dumps(value))
        result=roles.correct_session(self.root,self.backup,'2026-11-02');self.assertEqual((result['status'],result['qc']),('error','ERROR'))
    def test_existing_empty_yaml_does_not_invent_retention_or_validity(self):
        (self.root/'meta.yaml').write_bytes(b'')
        result=roles.correct_session(self.root,self.backup,'2026-11-02',syncer=lambda root:{'completed':True})
        self.assertIn('original_empty_meta_yaml_preserved',result['warnings']);self.assertEqual((self.root/'meta.yaml').read_bytes(),b'')
        self.assertEqual(json.loads((self.root/'meta.json').read_text(encoding='utf-8'))['device_config']['emg']['emg_left']['port'],'receiver-L')

if __name__=='__main__':unittest.main(verbosity=2)
