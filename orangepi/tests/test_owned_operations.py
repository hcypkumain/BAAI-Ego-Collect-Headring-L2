"""No hardware: admission and transaction tests for calibration/settings."""
import ast,json,pathlib,subprocess,sys,types,unittest,tempfile,shutil,os
from unittest.mock import Mock,patch
ROOT=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control'),str(ROOT.parent)]
import wrist_settings_worker as worker
from scan_process import ScanProcessError

class Tests(unittest.TestCase):
 def controller(self,runner):
  path=ROOT/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='advance_wrist_calibration']
  error=type('CalibrationError',(Exception,),{})
  calibration=types.SimpleNamespace(boot_id=lambda:'boot',current_stamp=lambda _:['stamp'],CalibrationError=error,PROMPTS={'retry':'retry','baseline':'baseline'})
  scope=dict(wrist_calibration=calibration,run_scan=runner,ScanProcessError=ScanProcessError,json=json,BASE=pathlib.Path('/tmp'),SRC=pathlib.Path('/tmp'),subprocess=subprocess)
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  c=scope['Controller']();c.remember_pending_owner=types.MethodType(worker.WebControls.remember_pending_owner,c);c.worker=None;c.camera_registry=object();c.stop_preview=Mock();c.state_write=Mock();c.voice=Mock()
  c.wrist_wizard=Mock();c.wrist_wizard.config={};c.wrist_wizard.phase='baseline';c.wrist_wizard.unchanged.return_value=True
  return c
 def test_calibration_success_uses_registered_runner(self):
  runner=Mock(return_value=(json.dumps(dict(ok=True,boot_id='boot',stamp=['stamp'],statistics={'ok':1})),0))
  c=self.controller(runner);c.advance_wrist_calibration();c.wrist_wizard.accept.assert_called_once_with({'ok':1});self.assertEqual(runner.call_args.kwargs['kind'],'calibration')
 def test_calibration_nonzero_exit_never_claims_success(self):
  runner=Mock(return_value=(json.dumps(dict(ok=True,boot_id='boot',stamp=['stamp'],statistics={'ok':1})),1))
  c=self.controller(runner);c.advance_wrist_calibration();c.wrist_wizard.accept.assert_not_called()
 def test_calibration_unreaped_group_blocks_success(self):
  runner=Mock(side_effect=ScanProcessError('未清理',True,123))
  c=self.controller(runner);c.advance_wrist_calibration();c.wrist_wizard.accept.assert_not_called();self.assertTrue(c.scan_cleanup_pending)
 def test_settings_transaction_keeps_lock_and_save(self):
  from contextlib import nullcontext
  reports={'cam_left_wrist':{'applied':True}}
  with patch.object(worker.settings,'preference_lock',return_value=nullcontext()),patch.object(worker.settings,'capture_guard',return_value=nullcontext()),patch.object(worker.settings,'configure_devices',return_value=reports) as cfg,patch.object(worker,'devices',return_value={}),patch.object(worker.WebControls,'save_wrist_settings') as save:
   self.assertEqual(worker.run(True),reports);cfg.assert_called_once();save.assert_called_once_with(True,reports)
 def test_settings_save_failure_reports_verified_rollback(self):
  from contextlib import nullcontext
  reports={'cam_left_wrist':{'device':'/dev/test','requested':True,'previous_actual_allow_auto_exposure_fps_change':False,'applied':True}}
  restored={'cam_left_wrist':{'applied':True,'actual_allow_auto_exposure_fps_change':False,'time':123}}
  with patch.object(worker.settings,'preference_lock',return_value=nullcontext()),patch.object(worker.settings,'capture_guard',return_value=nullcontext()),patch.object(worker.settings,'configure_devices',return_value=reports),patch.object(worker.settings,'save',side_effect=OSError('disk failure')),patch.object(worker.settings,'restore_devices',return_value=restored) as rollback,patch.object(worker,'devices',return_value={}):
   with self.assertRaisesRegex(ValueError,'未保存') as error:worker.run(True)
  rollback.assert_called_once();self.assertTrue(error.exception.reports['cam_left_wrist']['rolled_back']);self.assertFalse(error.exception.reports['cam_left_wrist']['actual_allow_auto_exposure_fps_change'])
 def test_preview_owner_not_lost_before_descendants_exit(self):
  from scan_registry import RegistryError
  import web_controls
  c=web_controls.WebControls();child=Mock();child.poll.return_value=0;c.preview_worker=child;c.preview_owner_run='current-run'
  c.camera_registry=Mock();c.camera_registry.finish.side_effect=[RegistryError('group alive'),None];c.preview_snapshot_id='snapshot';c.preview_sources={'a':'b'};c.emg_preview_sources={}
  self.assertFalse(c.stop_preview(clear=False));self.assertIs(c.preview_worker,child);self.assertEqual(c.preview_owner_run,'current-run')
  self.assertTrue(c.stop_preview(clear=False));self.assertIsNone(c.preview_worker);self.assertIsNone(c.preview_owner_run);self.assertEqual(c.camera_registry.finish.call_count,2)
 def test_same_run_late_cleanup_recovers_scan_calibration_and_settings(self):
  from scan_registry import RegistryError
  import web_controls
  for kind in ('scan','calibration','settings'):
   with self.subTest(kind=kind):
    c=web_controls.WebControls();c.camera_registry=Mock();c.camera_registry.finish.side_effect=[RegistryError('group alive'),None]
    child=Mock();error=ScanProcessError(kind+' pending',True,123,'same-run',child);c.remember_pending_owner(error)
    self.assertFalse(c.retry_pending_owner(force=True));self.assertEqual(c.pending_camera_owner[0],'same-run')
    self.assertTrue(c.retry_pending_owner(force=True));self.assertFalse(c.scan_cleanup_pending);self.assertEqual(c.camera_registry.finish.call_args.args,('same-run',));self.assertEqual(child.poll.call_count,2)
 def test_missing_run_retries_startup_reconciliation_instead_of_sticking(self):
  from scan_registry import RegistryError
  import web_controls
  c=web_controls.WebControls();c.camera_registry=Mock();c.launcher_lock=object()
  c.camera_registry.reconcile_startup.side_effect=[RegistryError('old owner remains'),None]
  c.remember_pending_owner(ScanProcessError('old owner remains',True,ownership_blocked=True))
  self.assertTrue(c.owner_reconcile_pending);self.assertIsNone(c.pending_camera_owner[0])
  self.assertFalse(c.retry_pending_owner(force=True));self.assertTrue(c.scan_cleanup_pending)
  self.assertTrue(c.retry_pending_owner(force=True));self.assertFalse(c.scan_cleanup_pending)
  self.assertEqual(c.camera_registry.reconcile_startup.call_count,2)
 def test_capture_late_cleanup_keeps_run_until_released(self):
  from scan_registry import RegistryError
  path=ROOT/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('owner_finished','_persist_owner_exit_proof')];scope={'RegistryError':RegistryError}
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope);c=scope['Controller']();c.capture_owner_run='same-run';c.capture_owner_released=False;c.camera_registry=Mock();c.state_write=Mock();c.last_help=[]
  record={'run':'same-run','nonce':'nonce','boot':'boot','kind':'capture','phase':'registered',
          'owner_pid':101,'owner_start':1001,'wrapper_pid':202,'wrapper_start':2002,'pgid':202,'sid':202}
  witness={'run':'same-run','nonce':'nonce','boot':'boot','owner_pid':101,'owner_start':1001}
  liveness=dict(record,liveness_verified=True,registry_cleanup_confirmed=True,group_absent=True)
  c.camera_registry.inspect.side_effect=[record,record,None]
  c.camera_registry.inspect_evidence.return_value={'witness':witness,'errors':[]}
  c.camera_registry.verify_exited.side_effect=[liveness,RegistryError('group alive'),liveness]
  c.camera_registry.finish.side_effect=[RegistryError('busy'),None]
  self.assertFalse(c.owner_finished());self.assertEqual(c.capture_owner_run,'same-run');self.assertTrue(c.owner_finished());self.assertIsNone(c.capture_owner_run)
 def test_real_late_grandchild_exit_releases_same_run_for_all_five_kinds(self):
  import ctypes,signal,time
  import scan_registry as registry_module
  import web_controls
  from owner_test_support import OwnerTestSupport
  libc=ctypes.CDLL(None,use_errno=True);old=ctypes.c_int()
  if libc.prctl(37,ctypes.byref(old),0,0,0)!=0:self.skipTest('subreaper unavailable')
  if libc.prctl(36,1,0,0,0)!=0:self.skipTest('subreaper unavailable')
  harness=OwnerTestSupport().start()
  try:
   for kind in ('scan','calibration','settings','preview','capture'):
    with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
     root=pathlib.Path(tmp);reg=registry_module.Registry(root);held=root/'held';held.write_text('ordinary');pidfile=root/'grandchild'
     grand="import os,sys,time;f=open(sys.argv[1]);open(sys.argv[2],'w').write(str(os.getpid()));time.sleep(30)"
     leader="import subprocess,sys,time,pathlib;subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[1],sys.argv[2]]);p=pathlib.Path(sys.argv[2]);\nwhile not p.exists():time.sleep(.01)"
     child,run=registry_module.spawn_owned(reg,kind,[sys.executable,'-c',leader,str(held),str(pidfile),grand],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
     try:
      child.wait(timeout=3);pid=int(pidfile.read_text());c=web_controls.WebControls();c.camera_registry=reg;c.state_write=Mock();c.last_help=[]
      if kind in ('scan','calibration','settings'):
       c.remember_pending_owner(ScanProcessError('pending',True,child.pid,run,child));attempt=lambda:c.retry_pending_owner(force=True)
      elif kind=='preview':
       c.preview_worker=child;c.preview_owner_run=run;c.preview_snapshot_id='x';c.preview_sources={};c.emg_preview_sources={};attempt=lambda:c.stop_preview(clear=False)
      else:
       path=ROOT/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller');cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('owner_finished','_persist_owner_exit_proof')];scope={'RegistryError':registry_module.RegistryError};exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
       c=scope['Controller']();c.camera_registry=reg;c.capture_owner_run=run;c.capture_owner_released=False;c.state_write=Mock();c.last_help=[];attempt=c.owner_finished
      self.assertFalse(attempt());self.assertEqual(reg.inspect()['run'],run)
      with self.assertRaises(registry_module.RegistryError):reg.reserve('scan')
      os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
      self.assertTrue(attempt());self.assertIsNone(reg.inspect())
      next_run=reg.reserve('scan');reg.cancel_unspawned(next_run)
     finally:
      if pidfile.exists():
       try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
       except ProcessLookupError:pass
      if child.stdin and not child.stdin.closed:child.stdin.close()
  finally:
   harness.stop();libc.prctl(36,old.value,0,0,0)
 def test_unregistered_scan_entry_refuses_before_device_probe(self):
  env=__import__('os').environ.copy();env['PYTHONPATH']=str(ROOT.parent);env.pop('ORANGEPI_CAMERA_OWNER_ROOT',None);env.pop('ORANGEPI_CAMERA_OWNER_RUN',None)
  result=subprocess.run([sys.executable,str(ROOT/'acquisition/scan_devices.py')],env=env,capture_output=True,text=True,timeout=4)
  self.assertNotEqual(result.returncode,0);self.assertIn('缺少本机登记',result.stderr)
 def test_fixed_wrapper_executes_only_registered_fixed_scan_path(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=pathlib.Path(tmp)/'orangepi';common=base/'src/orangepi_collect/common';control=base/'src/orangepi_collect/control';acq=base/'src/orangepi_collect/acquisition'
   for path in (common,control,acq,base/'.venvs/hardware/bin',base/'runtime'):path.mkdir(parents=True,exist_ok=True)
   for name in ('scan_registry.py','camera_owner_wrapper.py'):shutil.copy2(ROOT/'common'/name,common/name)
   shutil.copy2(ROOT/'control/scan_process.py',control/'scan_process.py')
   (common/'project_paths.py').write_text("from pathlib import Path\nCODE_BASE=Path(__file__).resolve().parents[3]\nBASE=CODE_BASE\nSRC=CODE_BASE/'src/orangepi_collect'\n")
   (acq/'scan_devices.py').write_text("from pathlib import Path\nimport sys\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]/'common'))\nfrom scan_registry import require_member\nrequire_member()\nprint('registered-scan')\n")
   # Test executable is copied under a supported basename; it is still the
   # host interpreter and does not claim to validate a real Python 3.8 runtime.
   runtime=pathlib.Path(tmp)/'python3.8';shutil.copy2(sys.executable,runtime)
   (base/'.venvs/hardware/bin/python').symlink_to(runtime)
   code="import sys,pathlib;sys.path[:0]=[sys.argv[1],sys.argv[2]];from scan_registry import Registry;from scan_process import run_scan;b=pathlib.Path(sys.argv[3]);print(run_scan([str(b/'.venvs/hardware/bin/python'),str(b/'src/orangepi_collect/acquisition/scan_devices.py')],Registry(b/'runtime'),timeout=3).strip())"
   result=subprocess.run([sys.executable,'-c',code,str(common),str(control),str(base)],capture_output=True,text=True,timeout=5)
   self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'registered-scan');self.assertFalse((base/'runtime/camera_owner.json').exists())

if __name__=='__main__':unittest.main(verbosity=2)
