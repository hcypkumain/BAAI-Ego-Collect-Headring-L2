import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import json,subprocess,tempfile,time,unittest,wave
from pathlib import Path
from unittest.mock import Mock,patch
import yaml
import ledger,launcher,speech_cloud

class LedgerTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime');self.root=Path(self.temp.name)
  self.lib=self.root/'configs/environments';self.scene=self.lib/'scene';self.scene.mkdir(parents=True)
  self.image=self.scene/'scene_0001.png';self.image.write_bytes(b'test')
  self.session=self.root/'data/run';self.session.mkdir(parents=True)
  self.patches=[patch.object(ledger,'BASE',self.root),patch.object(ledger,'PROJECT',self.root),patch.object(ledger,'LIBRARY',self.lib)]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.temp.cleanup()
 def test_success_then_failed_reenters_pool(self):
  ledger.atomic_yaml(self.session/'meta.yaml',{'status':'success'})
  ledger.mark(self.image,self.session,'success');self.assertEqual(ledger.unused(),[])
  data=ledger.read(self.scene/'used.yaml');self.assertEqual(set(data['used'][self.image.name]),{'session','at'})
  ledger.atomic_yaml(self.session/'meta.yaml',{'status':'failed'})
  self.assertEqual(ledger.unused(),[self.image])
  ledger.mark(self.image,self.session,'failed');self.assertNotIn(self.image.name,ledger.read(self.scene/'used.yaml')['used'])
 def test_failed_take_does_not_erase_other_success(self):
  ledger.mark(self.image,self.session,'success');ledger.mark(self.image,self.root/'different','failed')
  self.assertEqual(ledger.unused(),[])
 def test_bad_ledger_is_not_silently_overwritten(self):
  (self.scene/'used.yaml').write_text('used: []')
  with self.assertRaises(ValueError):ledger.unused()
 def test_combo_names_and_overviews(self):
  (self.scene/'0002_combo004_r2.png').touch();(self.scene/'placements_overview.png').touch()
  self.assertEqual(len(ledger.images()),2)

class CloudTests(unittest.TestCase):
 def test_timeout_fallback_then_background_recovers(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   root=Path(temp);(root/'configs').mkdir();(root/'configs/speech.yaml').write_text(yaml.safe_dump({'enabled':True,'model':'test','voice':'test','url':'test','foreground_wait_seconds':.01,'request_timeout_seconds':.01,'retry_initial_seconds':.03,'retry_max_seconds':.05}))
   calls=[]
   class Proc:
    def __init__(self,*a,**k):self.number=len(calls);calls.append(self);self.killed=False
    def poll(self):return None
    def kill(self):self.killed=True
    def communicate(self,input=None,timeout=None):
     if input is None:return ('','')
     if self.number==0:raise subprocess.TimeoutExpired('test',.01)
     req=json.loads(input)
     with wave.open(req['output'],'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*240)
     return ('{"ok":true}','')
   with patch.object(speech_cloud,'BASE',root),patch.object(speech_cloud.subprocess,'Popen',Proc):
    cloud=speech_cloud.CloudCache()
    try:
     self.assertIsNone(cloud.request('test',wait=0))
     path=cloud.request('test',wait=2)
     self.assertTrue(path.is_file());self.assertTrue(calls[0].killed)
     self.assertEqual(len(calls),2)
     self.assertEqual(cloud.request('test',wait=0),path);self.assertEqual(len(calls),2)
    finally:cloud.close()

class ContinuousTests(unittest.TestCase):
 def test_success_marks_used_and_leaves_next_task_for_operator(self):
  with tempfile.TemporaryDirectory(dir=launcher.BASE/'runtime') as temp:
   c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.smoke=0
   c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0;c.state='finalizing'
   c.notified_error=False;c.success_count=0;c.quit=False;c.selected_image=None;c.attempted=set()
   c.image=Path('first.png');c.images=[c.image,Path('next.png')];c.voice=Mock();c.qc_voice=Mock();c.reap=Mock();c.state_write=Mock();c.prepare=Mock()
   (c.path/'capture_summary.json').write_text(json.dumps({'status':'success','qc_level':'WARN'}))
   with patch('launcher.ledger.mark') as mark,patch('launcher.image_queue',return_value=[Path('next.png')]):
    c.poll();mark.assert_called_once_with(c.image,c.path,'success')
   self.assertFalse(c.quit);self.assertEqual(c.success_count,1);self.assertEqual(c.images,[Path('next.png')]);c.prepare.assert_not_called()
   self.assertEqual(c.state,'idle')
 def test_all_guidance_categories_can_be_spoken(self):
  c=launcher.Controller.__new__(launcher.Controller);c.voice=Mock()
  c.troubleshooting('emg eye hand_pose position ego_headband wristband cam marker')
  self.assertEqual(c.voice.say.call_count,9)
 def test_console_q_exits(self):
  keyboard=launcher.ConsoleKeyboard()
  with patch('launcher.select.select',return_value=([1],[],[])),patch('launcher.sys.stdin.readline',return_value='q\n'):
   self.assertEqual(keyboard.read(),'escape')

if __name__=='__main__':unittest.main()
