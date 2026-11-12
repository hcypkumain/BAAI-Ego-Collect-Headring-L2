import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

FILE=Path(__file__).resolve().parents[2]/'deployment/lan-time/headring_lan_time.py'
spec=importlib.util.spec_from_file_location('clock_test',FILE)
clock=importlib.util.module_from_spec(spec);spec.loader.exec_module(clock)

class LockClockTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  self.lock=self.root/'lock'; self.lock.touch(mode=0o644)
  self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps({'server':'127.0.0.1'}))
  self.patches=[patch.object(clock,'CONFIG',self.cfg),patch.object(clock,'ACQUISITION_LOCK',self.lock,create=True),patch.object(clock,'measure',return_value={'offset_seconds':3.,'rtt_seconds':.01,'server_stratum':1}),patch.object(clock.time,'clock_settime'),patch.object(clock.subprocess,'run')]
  self.mocks=[p.start() for p in self.patches];self.addCleanup(self.stop)
 def stop(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def test_boot_without_app_can_step(self):
  with patch.object(clock,'safe_to_step',return_value=(True,'boot')):
   result=clock.run(boot=True)
  self.assertEqual(result['status'],'aligned'); self.mocks[3].assert_called_once()
 def test_shared_acquisition_lock_defers_and_never_steps(self):
  with self.lock.open('r') as f:
   fcntl.flock(f,fcntl.LOCK_SH)
   with patch.object(clock,'safe_to_step',return_value=(True,'idle')):
    result=clock.run(boot=True)
  self.assertEqual(result['status'],'deferred');self.mocks[3].assert_not_called();self.mocks[4].assert_not_called()
 def test_missing_lock_fails_closed(self):
  self.lock.unlink()
  with patch.object(clock,'safe_to_step',return_value=(True,'idle')):
   result=clock.run(boot=True)
  self.assertEqual(result['status'],'deferred');self.mocks[3].assert_not_called()

 def test_existing_idle_ui_never_permits_step_even_if_status_is_idle(self):
  root=self.root/'app';(root/'runtime').mkdir(parents=True);(root/'runtime/dashboard_token').write_text('fixture')
  display={'state':'idle','session':None,'privacy':{'required':False},'worker':{'recording_active':False,'state':'ready'}}
  with patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(display))):
   self.assertFalse(clock.safe_to_step(boot=True)[0])
 def test_status_matrix_and_corruption_fail_closed(self):
  root=self.root/'app'; (root/'runtime').mkdir(parents=True); (root/'runtime/dashboard_token').write_text('fixture')
  session=self.root/'session';session.mkdir()
  display={'state':'ready','session':str(session),'privacy':{'required':False},'worker':{'recording_active':False,'state':'ready'}}
  cases=[({'active':True,'pending':False},False),({'active':False,'pending':True},False),({'active':True,'pending':True},False),({'active':False,'pending':False},False),({},False),({'active':0,'pending':False},False)]
  for impedance,expected in cases:
   with self.subTest(impedance=impedance):
    (session/'status.json').write_text(json.dumps({'eeg_impedance':impedance}))
    with patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(display))):
     self.assertEqual(clock.safe_to_step()[0],expected)
  for bad in ('', '{', '[]', '{}'):
   (session/'status.json').write_text(bad)
   with self.subTest(bad=bad),patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(display))):
    self.assertFalse(clock.safe_to_step()[0])
  (session/'status.json').unlink()
  with patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(display))):
   self.assertFalse(clock.safe_to_step(boot=True)[0])
  with patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',side_effect=OSError('HTTP failed')):
   self.assertFalse(clock.safe_to_step(boot=True)[0])
 def test_boot_allows_early_startup_server_while_exclusive(self):
  root=self.root/'app';(root/'runtime').mkdir(parents=True);(root/'runtime/dashboard_token').write_text('fixture')
  headers={'X-HeadRing-Startup':'1'}
  err=clock.urllib.error.HTTPError('http://localhost/',503,'starting',headers,None)
  with patch.object(clock,'BASE',root),patch.object(clock.urllib.request,'urlopen',side_effect=err):
   self.assertTrue(clock.safe_to_step(boot=True)[0]);self.assertFalse(clock.safe_to_step()[0])
 def test_boot_missing_ui_and_refused_connection(self):
  root=self.root/'app'; (root/'runtime').mkdir(parents=True)
  with patch.object(clock,'BASE',root):
   self.assertTrue(clock.safe_to_step(boot=True)[0]); self.assertFalse(clock.safe_to_step()[0])
   (root/'runtime/dashboard_token').write_text('fixture')
   with patch.object(clock.urllib.request,'urlopen',side_effect=clock.urllib.error.URLError(ConnectionRefusedError())):
    self.assertTrue(clock.safe_to_step(boot=True)[0]); self.assertFalse(clock.safe_to_step()[0])
 def test_clock_holds_exclusive_lock_through_actual_step_callback(self):
  import multiprocessing
  ctx=multiprocessing.get_context('fork'); entered=ctx.Event(); release=ctx.Event(); finished=ctx.Event()
  def step(*args): entered.set(); release.wait(4)
  def setter():
   with patch.object(clock,'safe_to_step',return_value=(True,'boot')),patch.object(clock.time,'clock_settime',side_effect=step):
    clock.run(boot=True)
   finished.set()
  p=ctx.Process(target=setter);p.start()
  try:
   self.assertTrue(entered.wait(3))
   with self.lock.open('r') as handle:
    with self.assertRaises(BlockingIOError):fcntl.flock(handle,fcntl.LOCK_SH|fcntl.LOCK_NB)
    release.set();self.assertTrue(finished.wait(3));p.join(3)
    fcntl.flock(handle,fcntl.LOCK_SH|fcntl.LOCK_NB)
   self.assertEqual(p.exitcode,0)
  finally:
   release.set();p.join(4)
   if p.is_alive():p.terminate();p.join()

if __name__=='__main__':unittest.main(verbosity=2)
