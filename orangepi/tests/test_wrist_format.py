import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from framework_qc import standardize, PiCameraChecker, CameraChecker


class WristFormatTests(unittest.TestCase):
    def test_host_arrival_timestamps_and_original_slot_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);slot='cam_left_wrist';directory=root/slot;directory.mkdir()
            stamps=[1700000000100000000,1700000000133333333]
            with (directory/'timestamps.csv').open('w',newline='') as file:
                writer=csv.writer(file)
                writer.writerow(['frame_index','frames_unix_ns','arrival_unix_ns','arrival_monotonic_ns','video_pts_ns','source_pts_ns'])
                for i,stamp in enumerate(stamps):writer.writerow([i,stamp,stamp,100+i*33333333,i*33333333,500+i*33333333])
            (directory/'frames.mp4').touch()
            (root/'capture_summary.json').write_text(json.dumps({'status':'success',slot:{'frames':2},
                'start_unix_ns':stamps[0]-100000000,'stop_unix_ns':stamps[-1]+100000000}))
            standardize(root)
            with np.load(directory/(slot+'.npz'),allow_pickle=False) as data:
                self.assertEqual(data['frames_timestamps'].dtype,np.float64)
                np.testing.assert_array_equal(data['frames_timestamps'],np.array(stamps,dtype=np.int64)/1e9)
                self.assertEqual(data['source_pts_ns'].dtype,np.int64)
                self.assertNotIn('ros_stamp_ns',data.files)
            meta=yaml.safe_load((root/'meta.yaml').read_text())
            self.assertEqual(meta['recorders'][slot],'Pi USB wrist camera')
            self.assertFalse((root/'cam_right_wrist').exists())

    def test_ros_checker_does_not_intercept_wrist_stream(self):
        self.assertFalse('cam_left_wrist'.startswith(PiCameraChecker.matches))
        self.assertTrue('cam_ros_left'.startswith(PiCameraChecker.matches))
        self.assertTrue('cam_left_wrist'.startswith(CameraChecker.matches))


if __name__=='__main__':unittest.main()
