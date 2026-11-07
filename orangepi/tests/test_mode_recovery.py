import ast,pathlib,subprocess,sys,threading,types,unittest
from unittest.mock import Mock,patch

ROOT=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'
def method(file,cls,name):
 tree=ast.parse((ROOT/file).read_text(encoding='utf-8'))
 c=next(x for x in tree.body if isinstance(x,ast.ClassDef)and x.name==cls)
 return next(x for x in c.body if isinstance(x,ast.FunctionDef)and x.name==name)
def compile_class(name,nodes,env):
 tree=ast.Module(body=[ast.ClassDef(name=name,bases=[],keywords=[],body=nodes,decorator_list=[])],type_ignores=[])
 ast.fix_missing_locations(tree);exec(compile(tree,'deployed-mode-methods','exec'),env);return env[name]

class BackendTests(unittest.TestCase):
 def dashboard(self):
  env={'snapshot_actions':lambda s:['collection_mode','start','prepare','delete_recording']}
  cls=compile_class('Dashboard',[method('dashboard.py','Dashboard','submit')],env)
  d=cls();d.lock=threading.RLock();d.snapshot={'collection_context':'emg:17','revision':5};d.busy=False;d.prompting=False;d.pending=None;d.result=None
  d.privacy=types.SimpleNamespace(snapshot=lambda:{'required':False},cancel_request=lambda:False)
  d.camera_checks=types.SimpleNamespace(approved=lambda:True)
  return d
 def test_mode_request_allows_only_metadata_revision_drift(self):
  d=self.dashboard();d.submit({'action':'collection_mode','value':'eeg','revision':4,'collection_context':'emg:17'})
  self.assertEqual(d.pending['value'],'eeg')
 def test_mode_request_from_old_profile_context_is_rejected(self):
  d=self.dashboard()
  with self.assertRaisesRegex(ValueError,'采集模式已变化'):d.submit({'action':'collection_mode','value':'eeg','revision':4,'collection_context':'emg:16'})
  self.assertIsNone(d.pending)
 def test_recording_and_deletion_still_reject_old_revision(self):
  for action in ['start','prepare','delete_recording']:
   d=self.dashboard()
   with self.assertRaisesRegex(ValueError,'流程已变化'):d.submit({'action':action,'revision':4,'collection_context':'emg:17'})
   self.assertIsNone(d.pending)
 def test_mode_cannot_bypass_busy_transaction_or_invalid_value(self):
  d=self.dashboard();d.busy=True
  with self.assertRaises(ValueError):d.submit({'action':'collection_mode','value':'eeg','revision':5,'collection_context':'emg:17'})
  d.busy=False
  with self.assertRaises(ValueError):d.submit({'action':'collection_mode','value':'invalid','revision':5,'collection_context':'emg:17'})
 def tick(self,action='collection_mode',context='emg:17'):
  cls=compile_class('Controls',[method('web_controls.py','WebControls','dashboard_tick')],{'actions_for':lambda *a:['collection_mode','start'],'subprocess':subprocess})
  c=cls();c.OwnerExited=type('OwnerExited',(Exception,),{});c.ui_revision=5;c.state='device_confirm';c.selected_devices=None;c.keyboard=None
  c.reference_tick=lambda:setattr(c,'ui_revision',6);c.voice=types.SimpleNamespace(tick=None,interrupt=Mock());c.ui_publish=Mock();c.ensure_preview=Mock();c.switch_collection_mode=Mock();c.key=Mock()
  c.dashboard=types.SimpleNamespace(take=lambda:{'action':action,'value':'eeg','revision':5,'collection_context':context},complete=Mock())
  return c
 def test_background_refresh_does_not_drop_accepted_mode_job(self):
  c=self.tick()
  with patch.dict(sys.modules,{'project_paths':types.SimpleNamespace(COLLECTION_CONTEXT='emg:17')}):c.dashboard_tick()
  c.switch_collection_mode.assert_called_once_with('eeg');c.dashboard.complete.assert_called_once_with(None)
 def test_queue_still_rejects_recording_revision_and_profile_changes(self):
  c=self.tick('start');c.dashboard_tick();c.key.assert_not_called();self.assertIn('流程已经变化',c.dashboard.complete.call_args.args[0])
  c=self.tick(context='emg:16')
  with patch.dict(sys.modules,{'project_paths':types.SimpleNamespace(COLLECTION_CONTEXT='emg:17')}):c.dashboard_tick()
  c.switch_collection_mode.assert_not_called();self.assertIn('采集模式已变化',c.dashboard.complete.call_args.args[0])
 def controller(self,worker=None,privacy=None):
  env={'COLLECTION_MODE':'emg','CODE_BASE':pathlib.Path('/unused'),'PROFILE':{'active_mode':'emg'}}
  cls=compile_class('Controller',[method('launcher.py','Controller','switch_collection_mode')],env)
  c=cls();c.state='device_confirm';c.worker=worker;c.quit=False;c.state_write=Mock();c.stop_preview=Mock()
  c.dashboard=types.SimpleNamespace(privacy=types.SimpleNamespace(snapshot=lambda:privacy or{'required':False,'stop_confirmed':True}),cloud_uploads=None)
  return c
 def test_actual_switch_still_blocks_worker_and_unconfirmed_screen(self):
  for c in [self.controller(worker=object()),self.controller(privacy={'required':True,'stop_confirmed':False}),self.controller(privacy={'required':False,'stop_confirmed':False})]:
   with patch.dict(sys.modules,{'collection_profiles':types.SimpleNamespace(provision=Mock())}):
    with self.assertRaises(ValueError):c.switch_collection_mode('eeg')
   self.assertFalse(c.quit)
 def test_actual_switch_and_selected_mode_noop(self):
  provision=Mock();c=self.controller()
  with patch.dict(sys.modules,{'collection_profiles':types.SimpleNamespace(provision=provision)}):c.switch_collection_mode('eeg')
  self.assertTrue(c.quit);self.assertEqual(c.mode_switch_target,'eeg');self.assertEqual(c.switch_requested,'eeg');provision.assert_called_once()
  c=self.controller();provision.reset_mock()
  with patch.dict(sys.modules,{'collection_profiles':types.SimpleNamespace(provision=provision)}):c.switch_collection_mode('emg')
  self.assertFalse(c.quit);provision.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
