"""Old STOP requests must not cancel another capture's arming phase."""
import ast
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/orangepi_collect/common'))
from screen_privacy import ScreenPrivacy

def submit_type():
 tree=ast.parse((ROOT/'src/orangepi_collect/control/dashboard.py').read_text())
 cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Dashboard')
 cls.bases=[];cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('submit','_queue_stop_locked')]
 scope={'snapshot_actions':lambda _:['stop','start']};exec(compile(ast.Module(body=[cls],type_ignores=[]),'dashboard.py','exec'),scope)
 return scope['Dashboard']

class Tests(unittest.TestCase):
 def setup_dashboard(self, committed=False):
  d=submit_type()();d.lock=threading.Lock();d.snapshot={'revision':25,'collection_context':'emg:3'}
  d.busy=False;d.prompting=False;d.pending=None;d.pending_stop=None;d.result=None
  d.privacy=ScreenPrivacy();d.privacy.phase='arming';d.privacy.session='/new';d.privacy.nonce='new';d.privacy.generation=7;d.privacy.committed=committed
  target={'instance':d.privacy.instance,'session_id':'/new','nonce':'new','generation':7}
  return d,{'action':'stop','revision':25,'collection_context':'emg:3','privacy_target':target}
 def test_old_revision_and_session_cannot_cancel(self):
  d,job=self.setup_dashboard();job.update(revision=1,privacy_target={**job['privacy_target'],'session_id':'/old','nonce':'old'})
  with self.assertRaises(ValueError):d.submit(job)
  self.assertFalse(d.privacy.cancelled);self.assertEqual(d.privacy.generation,7)
 def test_stale_identity_rejected_even_with_current_revision(self):
  d,job=self.setup_dashboard();job['privacy_target']['nonce']='old'
  with self.assertRaises(ValueError):d.submit(job)
  self.assertFalse(d.privacy.cancelled);self.assertEqual(d.privacy.generation,7)
 def test_valid_stop_cancels_arming_even_after_commit(self):
  for committed in (False,True):
   d,job=self.setup_dashboard(committed);d.submit(job)
   if committed:
    self.assertFalse(d.privacy.cancelled);self.assertEqual(d.privacy.phase,'stopping')
    self.assertEqual(d.pending_stop['action'],'stop')
   else:self.assertTrue(d.privacy.cancelled);self.assertEqual(d.privacy.generation,8)
 def test_old_generation_cannot_stop_new_state(self):
  d,job=self.setup_dashboard();d.privacy.generation+=1
  d.submit(job)
  self.assertFalse(d.privacy.cancelled);self.assertEqual(d.pending_stop['action'],'stop')
 def test_legacy_stop_without_identity_requires_refresh(self):
  d,job=self.setup_dashboard(True);del job['privacy_target']
  with self.assertRaisesRegex(ValueError,'刷新'):
   d.submit(job)
  self.assertFalse(d.privacy.cancelled)
 def test_recording_stop_queues_new_stopping_generation(self):
  d,job=self.setup_dashboard(True);d.privacy.phase='recording';d.busy=False;d.prompting=False;d.pending=None
  d.submit(job)
  self.assertEqual(d.privacy.phase,'stopping')
  self.assertTrue(d.privacy.target_matches(d.pending_stop['privacy_target']))
  self.assertEqual(d.pending_stop['privacy_target']['generation'],8)
 def test_legacy_start_is_rejected_before_camera_or_pending(self):
  d,job=self.setup_dashboard();d.camera_checks=SimpleNamespace(approved=lambda: (_ for _ in ()).throw(AssertionError('camera check should not run')))
  with self.assertRaisesRegex(ValueError,'刷新'):
   d.submit({'action':'start','revision':25,'collection_context':'emg:3'})
 def test_legacy_display_cannot_ack_or_join_new_start(self):
  clock=[100.0];p=ScreenPrivacy(clock=lambda:clock[0]);p.display({'client_id':'legacy'})
  with self.assertRaisesRegex(ValueError,'刷新'):p.require_stop_protocol()
  p.arm('/test');nonce=p.nonce
  with self.assertRaisesRegex(ValueError,'刷新'):p.acknowledge({'client_id':'legacy','nonce':nonce})
  self.assertFalse(p.ready())
 def test_modern_display_ack_is_accepted(self):
  clock=[100.0];p=ScreenPrivacy(clock=lambda:clock[0]);p.display({'client_id':'new','stop_protocol':'session-stop-v1'})
  p.require_stop_protocol();p.arm('/test');p.acknowledge({'client_id':'new','nonce':p.nonce,'stop_protocol':'session-stop-v1'})
  clock[0]+=.3;self.assertTrue(p.ready())
if __name__=='__main__':unittest.main()
