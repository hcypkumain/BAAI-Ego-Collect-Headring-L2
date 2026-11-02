"""Photo export after5seconds requires a newly published complete frame set."""
import json,os,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
from camera_check import CameraChecks,CameraCheckError
from PIL import Image

class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.preview=Path(self.tmp.name)
        self.owner=SimpleNamespace(lock=threading.Lock(),preview=self.preview,
            snapshot={'state':'ready','session':'session','selected_devices':['cam_ros_left'],
                'devices':[{'name':'cam_ros_left','connected':True,'config':{'topic':'/camera'}}],
                'worker':{'pid':123,'state':'ready','recording_active':False}},
            privacy=SimpleNamespace(snapshot=lambda:{'phase':'idle','stop_confirmed':True}))
        self.check=CameraChecks(self.owner)
    def frame(self,when,color='green'):
        path=self.preview/'cam_ros_left.jpg';Image.new('RGB',(100,80),color).save(path,'JPEG');os.utime(path,(when,when))
    def test_existing_cached_photo_is_not_reused_for_after_deadline_request(self):
        self.frame(99.9)
        with patch('camera_check.time.time',return_value=100):
            old,_=self.check.sheet(2)
            with self.assertRaises(CameraCheckError) as caught:self.check.sheet(2,after_unix_ms=100000)
        self.assertEqual(caught.exception.status,425)
        self.frame(100.2,'blue')
        with patch('camera_check.time.time',return_value=100.3):new,meta=self.check.sheet(2,after_unix_ms=100000)
        self.assertNotEqual(old,new);self.assertTrue(meta['complete']);self.assertGreaterEqual(min(meta['frame_preview_mtime_ms']),100000)
    def test_missing_camera_does_not_export_an_incomplete_photo(self):
        with patch('camera_check.time.time',return_value=100):
            with self.assertRaises(CameraCheckError) as caught:self.check.sheet(2,after_unix_ms=100000)
        self.assertEqual(caught.exception.status,425)
    def test_request_latches_server_receive_time_and_waits_for_a_later_frame(self):
        self.frame(100.2)
        with patch('camera_check.time.time',return_value=100.3):
            with self.assertRaises(CameraCheckError):self.check.sheet(2,after_unix_ms=100000,request_id='fresh_request')
        self.frame(100.5,'blue')
        with patch('camera_check.time.time',return_value=100.6):_,meta=self.check.sheet(2,after_unix_ms=100000,request_id='fresh_request')
        self.assertEqual(meta['photo_not_before_unix_ms'],100300);self.assertGreaterEqual(min(meta['frame_preview_mtime_ms']),100300)
    def test_capture_transition_discards_photo(self):
        self.frame(100.2);original=self.check.context;calls=[0]
        def context():
            value=original();calls[0]+=1
            if calls[0]>1:return value[0],value[1],value[2],False,value[4]
            return value
        with patch.object(self.check,'context',side_effect=context),patch('camera_check.time.time',return_value=100.3):
            with self.assertRaises(CameraCheckError) as caught:self.check.sheet(2,after_unix_ms=100000)
        self.assertEqual(caught.exception.status,423)

if __name__=='__main__':unittest.main()
