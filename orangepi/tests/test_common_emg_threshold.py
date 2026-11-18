import tempfile,unittest
from pathlib import Path
import numpy as np
from embodied_brain_collect.checkers.emg import EmgChecker
from embodied_brain_collect.checkers.camera import CameraChecker
from framework_qc import _emg_checker_config

class GapTest(unittest.TestCase):
 def test_real_checkers_use_scoped_gap_and_preserve_sn_loss(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);emg=root/'emg_left';emg.mkdir();cam=root/'cam_ros_left';cam.mkdir()
   for gap,emg_error in ((.12,False),(.15,False),(.151,True)):
    t=np.arange(100,dtype=np.float64)*.01;t[50:]+=gap-.01
    np.savez(emg/'data.npz',emg_timestamps=t,imu_timestamps=t,emg_sn=np.arange(100)*2,imu_sn=np.arange(100)*2+1)
    np.savez(cam/'data.npz',frames_timestamps=t)
    cfg={'timestamp_gap':{'min_s':.1,'mult':1.5}}
    result=EmgChecker(_emg_checker_config(cfg)).run(emg)
    self.assertEqual(any(x.check=='TimestampGap' and x.level=='ERROR' for x in result.findings),emg_error)
    self.assertTrue(any(x.check=='TimestampGap' and x.level=='ERROR' for x in CameraChecker(cfg).run(cam).findings))
   # Sequence number loss remains an independent check.
   np.savez(emg/'data.npz',emg_timestamps=np.arange(100)*.01,imu_timestamps=np.arange(2)*.01,emg_sn=np.arange(100)*2,imu_sn=np.arange(2)*2+1)
   result=EmgChecker(_emg_checker_config(cfg)).run(emg)
   self.assertTrue(any(x.check=='SnGap' for x in result.findings))

if __name__=='__main__':unittest.main()
