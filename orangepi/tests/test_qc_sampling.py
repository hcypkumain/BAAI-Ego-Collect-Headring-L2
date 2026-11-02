import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from embodied_brain_collect.checkers.checks import decode_video

class SampledVideoTests(unittest.TestCase):
    def test_decodes_all_frames_but_only_materializes_sampled_images(self):
        class Capture:
            index=0;retrieved=[]
            def isOpened(self):return True
            def get(self,key):return 20
            def grab(self):self.index+=1;return self.index<=40
            def retrieve(self):
                self.retrieved.append(self.index)
                return True,np.full((4,4,3),self.index,dtype=np.uint8)
            def release(self):pass
        cap=Capture();ts=np.arange(40)/20+100
        with patch('cv2.VideoCapture',return_value=cap):
            result=decode_video(Path('clip.mp4'),ts,{'t0':100,'t1':102})
        self.assertEqual(result.n_frames,40);self.assertEqual(cap.retrieved,[1,11,21,31])
        np.testing.assert_array_equal(result.lums,[1,11,21,31])
        np.testing.assert_array_equal(result.diffs,[10,10,10])
        np.testing.assert_array_equal(result.t_samp,ts[[0,10,20,30]])

if __name__=='__main__':unittest.main()
