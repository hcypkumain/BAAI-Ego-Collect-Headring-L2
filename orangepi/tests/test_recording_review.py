"""Finished-video access and prompt-free completion of durable saving."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard
import launcher
import recording_review

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        (self.base/'runtime').mkdir();self.session=self.base/'session-one';self.session.mkdir()
        (self.session/'stop_gate.json').write_text('{}')
        camera=self.session/'cam_left_wrist';camera.mkdir()
        (camera/'frames.mp4').write_bytes(bytes(range(128)))
        (camera/'video_check.json').write_text(json.dumps({'streams':[{'duration':'3.2'}]}))
        self.ui=dashboard.Dashboard(self.base,self.base,{'host':'127.0.0.1','port':0})
        self.ui.publish({'state':'confirm_stop','revision':1,'review_session':str(self.session)})
        self.url='http://127.0.0.1:'+str(self.ui.server.server_port)+'/api/video/cam_left_wrist?session=session-one'
    def tearDown(self):self.ui.close();self.temp.cleanup()
    def get(self,range=None,url=None,auth=True,method=None):
        headers={'X-Access-Token':self.ui.token} if auth else {}
        if range:headers['Range']=range
        return urlopen(Request(url or self.url,headers=headers,method=method),timeout=3)
    def test_video_requires_authentication(self):
        with self.assertRaises(HTTPError) as e:self.get(auth=False)
        self.assertEqual(e.exception.code,401)
    def test_video_seeks_with_bounded_and_suffix_ranges(self):
        for header,wanted,span in [('bytes=7-15',bytes(range(7,16)),'bytes 7-15/128'),('bytes=-3',bytes([125,126,127]),'bytes 125-127/128')]:
            with self.get(header) as r:
                self.assertEqual(r.status,206);self.assertEqual(r.headers['Content-Range'],span);self.assertEqual(r.read(),wanted)
        with self.get(method='HEAD') as r:
            self.assertEqual(r.headers['Content-Length'],'128');self.assertEqual(r.read(),b'')
    def test_invalid_range_and_session_or_path_are_rejected(self):
        for header in ('bytes=999-','bytes=1-2,5-6','bytes=8-2','bytes=-0'):
            with self.assertRaises(HTTPError) as e:self.get(header)
            self.assertEqual(e.exception.code,416)
        for url in (self.url.replace('session-one','other-session'),self.url.replace('cam_left_wrist','..%2Fruntime%2Fdashboard_token')):
            with self.assertRaises(HTTPError) as e:self.get(url=url)
            self.assertEqual(e.exception.code,404)
    def test_video_unavailable_until_container_closed_and_symlinks_rejected(self):
        camera=self.session/'cam_left_wrist';(camera/'video_check.json').unlink()
        with self.assertRaises(HTTPError) as e:self.get()
        self.assertEqual(e.exception.code,404)
        (camera/'video_check.json').write_text('{}');(camera/'frames.mp4').unlink()
        (camera/'frames.mp4').symlink_to(self.base/'runtime/dashboard_token')
        with self.assertRaises(HTTPError) as e:self.get()
        self.assertEqual(e.exception.code,404)
    def test_review_does_not_confirm_save_or_expose_private_paths(self):
        s=self.ui.status();self.assertNotIn('review_session',s)
        self.assertEqual(s['review']['videos'][0]['duration'],3.2)
        with self.get() as r:self.assertEqual(len(r.read()),128)
        self.assertIsNone(self.ui.take());self.assertEqual(self.ui.status()['state'],'confirm_stop')

class SaveCompletionTests(unittest.TestCase):
    def test_warning_speech_does_not_delay_commit_or_start_next_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.smoke=0
            c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0
            c.state='finalizing';c.notified_error=False;c.success_count=0;c.quit=False
            c.image=Path('task.png');c.images=[c.image,Path('next.png')]
            c.voice=Mock();c.qc_voice=Mock();c.state_write=Mock();c.prepare=Mock()
            c.reap=Mock();c.worker_status=Mock(return_value={'state':'success'})
            (c.path/'meta.yaml').write_text('{}')
            (c.path/'capture_summary.json').write_text(json.dumps({'status':'success','qc_level':'WARN','warnings':['cam_left_wrist: frame timestamp gap ns=90000000']}))
            with patch('launcher.ledger.mark') as mark:
                def spoken(text):
                    mark.assert_called_once_with(c.image,c.path,'success')
                    self.assertEqual(c.state,'idle');self.assertIn('已保存',text)
                c.voice.say.side_effect=spoken;c.poll()
            c.qc_voice.assert_not_called();c.prepare.assert_not_called()
        self.assertEqual(c.success_count,1);c.reap.assert_called_once()

if __name__=='__main__':unittest.main()
