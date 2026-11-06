"""Behavior checks for the EEG dispatcher plus shared original EMG rules."""
import json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
import numpy as np
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/orangepi_collect/common'))
import project_paths
import framework_qc as qc
from embodied_brain_collect.checkers.base import StreamReport,Finding
from embodied_brain_collect.session.config import load_checker
from framework_recorder import impedance_request

class EegQcIntegration(unittest.TestCase):
 def test_selected_eeg_is_dispatched_to_the_pi_eeg_checker(self):
  with tempfile.TemporaryDirectory() as temp:
   root=pathlib.Path(temp);(root/'eeg').mkdir()
   np.savez(root/'eeg/eeg.npz',eeg_data=np.zeros((8,8)))
   (root/'device_selection.json').write_text(json.dumps({'selected':['eeg'],'device_config':{}}))
   qc.standardize_marker(root,{'start_unix_ns':0,'stop_unix_ns':1000000000})
   expected=StreamReport(stream='eeg',findings=[Finding('WARN','EEG-specific finding',check='EEGFixture')])
   with patch.object(qc.PiEegChecker,'run',return_value=expected) as checker:
    result=qc.parallel_qc_session(root,load_checker(),workers=1,only=['eeg'])
   checker.assert_called_once();self.assertEqual(result.streams['eeg'].level,'WARN')
   self.assertEqual(result.streams['eeg'].findings[0].check,'EEGFixture')

 def test_impedance_end_remains_an_impedance_envelope(self):
  value=impedance_request(json.dumps({'command':'IMPEDANCE_END','request_id':'test'}))
  self.assertEqual(json.loads(value),{'command':'IMPEDANCE_END','request_id':'test'})
  self.assertIsNone(impedance_request('STOP'))
  self.assertIsNone(impedance_request('{"command":"STOP","request_id":"test"}'))

 def test_shared_emg_gap_rules_still_apply_in_the_eeg_edition(self):
  for gap,expected in [(.02,'WARN'),(.12,'ERROR')]:
   with self.subTest(gap=gap),tempfile.TemporaryDirectory() as temp:
    root=pathlib.Path(temp);d=root/'emg_left';d.mkdir()
    k=np.arange(15000);em=k%10!=9;et=k[em]*.00045;it=k[~em]*.00045;et[len(et)//2:]+=gap
    np.savez(d/'emg_left.npz',emg_timestamps=et,imu_timestamps=it,emg_sn=k[em]%256,imu_sn=k[~em]%256,
             emg_data=np.zeros((len(et),8)),imu_gyro=np.zeros((len(it),3)),imu_accel=np.zeros((len(it),3)))
    (root/'device_selection.json').write_text(json.dumps({'selected':['emg_left'],'device_config':{}}))
    qc.standardize_marker(root,{'start_unix_ns':0,'stop_unix_ns':int(min(et[-1],it[-1])*1e9)})
    report=qc.check_emg(root)
    self.assertEqual(report['level'],expected)
    self.assertEqual(json.loads((root/'emg_qc_policy.json').read_text())['accepted'],expected!='ERROR')

if __name__=='__main__':unittest.main()
