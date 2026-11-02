import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import csv,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np
import yaml
import framework_qc
import framework_recorder
import launcher
from embodied_brain_collect.recorders.base import BaseRecorder

class InterfaceTests(unittest.TestCase):
 def test_recorder_contract(self):
  self.assertTrue(issubclass(framework_recorder.PiBundleRecorder,BaseRecorder))
 def test_selected_image_filename(self):
  queue=launcher.image_queue('餐桌_倒水_0003.png')
  self.assertGreater(len(queue),1);self.assertEqual(queue[0].name,'餐桌_倒水_0003.png')
 def test_selected_image_rejects_outside_library(self):
  with self.assertRaises(ValueError):launcher.image_queue('/etc/passwd')
 def test_unknown_image_rejected(self):
  with self.assertRaises(ValueError):launcher.image_queue('not_an_existing_reference_9999.png')
 def test_image_choice_does_not_consume_ledger(self):
  ledger=launcher.BASE/'runtime'/'used.json'
  before=ledger.read_bytes() if ledger.exists() else None
  launcher.image_queue('餐桌_倒水_0003.png')
  self.assertEqual(before,ledger.read_bytes() if ledger.exists() else None)
 def test_completed_image_exits(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.smoke=1
   c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0
   c.state='finalizing';c.notified_error=False;c.success_count=0;c.quit=False
   c.voice=Mock();c.qc_voice=Mock();c.reap=Mock();c.state_write=Mock()
   (c.path/'capture_summary.json').write_text(json.dumps({'status':'success','qc_level':'INFO'}))
   c.poll();self.assertTrue(c.quit);self.assertEqual(c.success_count,1)
 def test_qc_error_does_not_consume_image_and_exits(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.smoke=1
   c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0
   c.state='finalizing';c.notified_error=False;c.success_count=0;c.quit=False
   c.error_voice=Mock();c.qc_voice=Mock();c.reap=Mock();c.state_write=Mock()
   c.voice=Mock()
   (c.path/'capture_summary.json').write_text(json.dumps({'status':'qc_error','errors':['QC ERROR']}))
   c.poll();self.assertTrue(c.quit);self.assertEqual(c.success_count,0);c.error_voice.assert_called_once()

class RetryTests(unittest.TestCase):
 def test_retry_keeps_image_and_does_not_exit_or_consume(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.smoke=0
   c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0
   c.state='finalizing';c.notified_error=False;c.success_count=0;c.quit=False
   c.image=Path('same.png');c.images=[c.image];c.retry_requested=True
   c.voice=Mock();c.qc_voice=Mock();c.reap=Mock();c.state_write=Mock();c.prepare=Mock()
   (c.path/'capture_summary.json').write_text(json.dumps({'status':'success','qc_level':'INFO'}))
   with patch('launcher.ledger.mark') as mark:
    c.poll();mark.assert_called_once_with(c.image,c.path,'failed')
   self.assertFalse(c.quit);self.assertEqual(c.success_count,0)
   self.assertEqual(c.images,[c.image]);c.prepare.assert_called_once()
   self.assertTrue((c.path/'retry.json').exists())

class DataContractTests(unittest.TestCase):
 def test_standard_output_and_yaml_scalars(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   root=Path(temp);start=1700000000.
   summary={'status':'success','start_unix_ns':int(start*1e9),'stop_unix_ns':int((start+1)*1e9)}
   for name in ('emg_left','emg_right'):
    directory=root/name;directory.mkdir();summary[name]={'emg_frames':3}
    ts=np.array([start+.1,start+.2,start+.3])
    np.savez(directory/'chunk_000000.npz',emg_arrival_timestamps=ts,emg_timestamps=ts,
     emg_sn=np.array([1,3,5]),emg_data=np.ones((3,8)),imu_arrival_timestamps=ts+.01,
     imu_timestamps=ts+.01,imu_sn=np.array([2,4,6]),imu_gyro=np.ones((3,3)),imu_accel=np.ones((3,3)))
   (root/'capture_summary.json').write_text(json.dumps(summary))
   (root/'meta.json').write_text(json.dumps({'reference_image':'configs/environments/example.png'}))
   framework_qc.standardize(root)
   meta=yaml.safe_load((root/'meta.yaml').read_text())
   self.assertIsInstance(meta['start_time'],float)
   self.assertFalse(meta['pack_episode'])
   self.assertEqual(meta['environment'],'example.png')
   self.assertNotIn('pygame',sys.modules)
   self.assertEqual(meta['recorders']['emg_left'],'WLEMGUV2A')
   with np.load(root/'marker'/'marker.npz') as z:
    self.assertEqual(z['marker_tag'].tolist(),['RUN_START','RUN_END'])
   for name in ('emg_left','emg_right'):
    self.assertEqual(len(list((root/name).glob('*.npz'))),1)
    self.assertTrue((root/name/'raw_chunks'/'chunk_000000.npz').exists())
    with np.load(root/name/(name+'.npz')) as arrays:self.assertEqual(arrays['emg_data'].shape,(3,8))
   from embodied_brain_collect.checkers.marker import find_run_window
   window=find_run_window(root);self.assertEqual(window['t0'],start);self.assertEqual(window['t1'],start+1)

if __name__=='__main__':unittest.main()
