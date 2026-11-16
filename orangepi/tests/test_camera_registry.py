"""Crash/restart ownership tests using ordinary local processes and files."""
import json,os,pathlib,signal,subprocess,sys,tempfile,time,unittest,ast,types
from unittest.mock import patch,Mock
ROOT=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
sys.path[:0]=[str(ROOT),str(ROOT.parent/'control')]
import scan_registry as r
import scan_process
from owner_test_support import OwnerTestSupport

class RegistryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);self.reg=r.Registry(self.root)
  self.harness=OwnerTestSupport().start();self.addCleanup(self.harness.stop)
 def start(self,code,*args):
  return subprocess.Popen([sys.executable,'-c',code,str(ROOT),str(self.root),*map(str,args)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 def test_reserved_survives_controller_death(self):
  code="import sys,signal;sys.path.insert(0,sys.argv[1]);import scan_registry as r;x=r.Registry(sys.argv[2]);x.reserve('scan');print('reserved',flush=True);signal.pause()"
  proc=self.start(code);self.assertEqual(proc.stdout.readline().strip(),'reserved');proc.kill();proc.wait();proc.stdout.close();proc.stderr.close()
  with self.assertRaises(r.RegistryError):self.reg.reserve('preview')
  with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertEqual(self.reg.inspect()['phase'],'reserved')
 def test_real_wrapper_reservation_blocks_restart_until_group_gone(self):
  code="import sys,time;sys.path.insert(0,sys.argv[1]);import scan_registry as r;r.validate_owned_command=lambda *args:None;r.WRAPPER_PATH=__import__('pathlib').Path(sys.argv[3]);x=r.Registry(sys.argv[2]);p,run=r.spawn_owned(x,'scan',[sys.executable,'-c','import time;time.sleep(30)']);print(p.pid,flush=True);time.sleep(30)"
  controller=self.start(code,self.harness.path/'camera_owner_wrapper.py');pid=int(controller.stdout.readline());deadline=time.monotonic()+3
  while time.monotonic()<deadline:
   val=self.reg.inspect()
   if val.get('phase')=='registered':break
   time.sleep(.01)
  self.assertEqual(val['phase'],'registered');controller.kill();controller.wait();controller.stdout.close();controller.stderr.close()
  try:
   with self.assertRaises(r.RegistryError):self.reg.reserve('capture')
  finally:
   # Test alone owns this freshly created process group, never a historical PID.
   os.killpg(pid,signal.SIGKILL)
  deadline=time.monotonic()+3
  while time.monotonic()<deadline and r.group_exists(pid):time.sleep(.02)
  if r.group_exists(pid):
   # Unreaped zombies remain conservative barriers on some container PID 1s.
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
  else:
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
   self.assertIsNotNone(self.reg.inspect())
 def test_only_one_concurrent_reservation(self):
  run=self.reg.reserve('scan')
  with self.assertRaises(r.RegistryError):self.reg.reserve('preview')
  self.assertEqual(self.reg.inspect()['run'],run)
 def test_corrupt_reserved_and_boot_failure_closed(self):
  self.reg.record.write_text('{')
  with self.assertRaises(r.RegistryError):self.reg.reserve('scan')
  self.reg.record.unlink();self.reg.reserve('scan')
  with patch.object(r,'boot_id',return_value=None):
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
 def test_boot_change_does_not_resolve_incomplete_reservation(self):
  self.reg.reserve('scan')
  with patch.object(r,'boot_id',return_value='11111111-1111-4111-8111-111111111111'):
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertIsNotNone(self.reg.inspect())
 def test_old_run_cannot_clear_new_run(self):
  run=self.reg.reserve('scan')
  with self.assertRaises(r.RegistryError):self.reg.finish('old-run')
  self.assertEqual(self.reg.inspect()['run'],run)
 def test_permission_denied_group_probe_blocks(self):
  with patch.object(r.os,'killpg',side_effect=PermissionError()):
   with self.assertRaises(r.RegistryError):r.group_exists(123)
 def test_reserved_missing_wrapper_never_reconciles_same_boot(self):
  self.reg.reserve('preview')
  with patch.object(r,'proc_identity',return_value=None):
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
 def test_write_failure_fails_before_spawn(self):
  with patch.object(self.reg,'_write',side_effect=OSError('disk failed')):
   with self.assertRaises(OSError):r.spawn_owned(self.reg,'scan',[sys.executable,'-c','raise SystemExit(97)'])
 def test_existing_owner_barrier_is_not_misreported_as_our_pending_child(self):
  with patch.object(scan_process,'spawn_owned',side_effect=r.RegistryError('historical owner remains')):
   with self.assertRaises(scan_process.ScanProcessError) as caught:
    scan_process.run_scan([sys.executable,'-c','pass'],self.reg)
  self.assertFalse(caught.exception.cleanup_pending);self.assertTrue(caught.exception.ownership_blocked)
  self.assertIsNone(caught.exception.run);self.assertIsNone(caught.exception.child)
 def test_late_wrong_uuid_registration_rejected(self):
  self.reg.reserve('scan')
  with self.assertRaises(r.RegistryError):self.reg.register('wrong-id')
 def test_wrapper_registration_failure_never_executes_probe(self):
  run=self.reg.reserve('scan');sentinel=self.root/'probe-started'
  wrapper=ROOT/'camera_owner_wrapper.py'
  proc=subprocess.run([sys.executable,str(wrapper),str(self.root),'wrong-run',sys.executable,'-c',"open(%r,'w').write('unsafe')"%str(sentinel)],start_new_session=True,capture_output=True,text=True,timeout=3)
  self.assertNotEqual(proc.returncode,0);self.assertFalse(sentinel.exists());self.assertEqual(self.reg.inspect()['run'],run)
 def test_registered_wrapper_dead_absent_group_auto_reconciles(self):
  run=self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with patch.object(r,'proc_identity',return_value=None),patch.object(r,'group_exists',return_value=False):
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertIsNotNone(self.reg.inspect())
 def test_registered_wrapper_live_never_reconciles(self):
  self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with patch.object(r,'proc_identity',side_effect=[None,dict(start=999,state='D')]):
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
 def test_old_uuid_cannot_finish_new_registered_run(self):
  self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with self.assertRaises(r.RegistryError):self.reg.finish('previous-run')
  self.assertEqual(self.reg.inspect()['run'],value['run'])
 def test_spawn_and_prepare_reservations_are_mutually_exclusive(self):
  self.reg.reserve('scan')
  for kind in ('scan','preview','capture'):
   with self.assertRaises(r.RegistryError):self.reg.reserve(kind)

 def test_corrupted_boot_is_not_accepted_or_deleted(self):
  self.reg.reserve('scan');value=json.loads(self.reg.record.read_text());value['boot']='invalid';self.reg.record.write_text(json.dumps(value))
  with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertTrue(self.reg.record.exists())
 def test_replaced_valid_boot_fails_integrity_check(self):
  self.reg.reserve('scan');value=json.loads(self.reg.record.read_text());value['boot']='11111111-1111-4111-8111-111111111111';self.reg.record.write_text(json.dumps(value))
  with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertTrue(self.reg.record.exists())
 def test_matching_live_owner_blocks_even_if_boot_field_changed_with_recalculated_checksum(self):
  self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(boot='11111111-1111-4111-8111-111111111111',phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with self.assertRaises(r.RegistryError):self.reg.reconcile()
  self.assertIsNotNone(self.reg.inspect())
 def test_pid_reuse_does_not_signal_or_reconcile_live_group(self):
  self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with patch.object(r,'proc_identity',return_value=dict(start=777,state='S')),patch.object(r.os,'killpg',return_value=None) as kill:
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
   kill.assert_not_called()
  self.assertIsNotNone(self.reg.inspect())
 def test_wrapper_fsync_failure_never_starts_probe(self):
  run=self.reg.reserve('scan');sentinel=self.root/'probe-started'
  code="import sys,os;sys.path.insert(0,sys.argv[1]);import scan_registry as r;r.os.fsync=lambda fd:(_ for _ in ()).throw(OSError('fsync failed'));r.Registry(sys.argv[2]).register(sys.argv[3]);open(sys.argv[4],'w').write('unsafe')"
  proc=self.start(code,run,sentinel);proc.wait(timeout=3);proc.stdout.close();proc.stderr.close()
  self.assertNotEqual(proc.returncode,0);self.assertFalse(sentinel.exists());self.assertEqual(self.reg.inspect()['phase'],'reserved')
 def test_two_real_controllers_only_one_reserves(self):
  barrier=self.root/'go'
  code="import sys,time,pathlib;sys.path.insert(0,sys.argv[1]);import scan_registry as r;x=r.Registry(sys.argv[2]);b=pathlib.Path(sys.argv[3]);\nwhile not b.exists():time.sleep(.005)\ntry:x.reserve('scan');print('won',flush=True);time.sleep(.3)\nexcept r.RegistryError:print('blocked',flush=True)"
  one=self.start(code,barrier);two=self.start(code,barrier);barrier.write_text('go')
  a=one.communicate(timeout=3)[0].strip();b=two.communicate(timeout=3)[0].strip()
  self.assertCountEqual([a,b],['won','blocked'])

 def test_normal_preview_capture_scan_ownership_handoffs(self):
  for kind in ('preview','capture','scan'):
   p,run=r.spawn_owned(self.reg,kind,[sys.executable,'-c','import time;time.sleep(.12)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   deadline=time.monotonic()+2
   while time.monotonic()<deadline:
    value=self.reg.inspect()
    if value and value.get('phase')=='registered':break
    time.sleep(.005)
   self.assertEqual(self.reg.verify_owner(run,kind)['kind'],kind)
   for other in ('preview','capture','scan'):
    with self.assertRaises(r.RegistryError):self.reg.reserve(other)
   p.wait(timeout=2);self.reg.finish(run);self.assertIsNone(self.reg.inspect())

 def test_discard_never_deletes_while_owned_grandchild_holds_file(self):
  parent=ROOT.parent;path=parent/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('owner_finished','reap','poll_discard')]
  scope=dict(RegistryError=r.RegistryError,time=time,os=os,signal=signal);exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  held=self.root/'held';held.write_text('evidence');pidfile=self.root/'childpid'
  grand="import os,sys,time;f=open(sys.argv[1]);open(sys.argv[2],'w').write(str(os.getpid()));time.sleep(30)"
  leader="import subprocess,sys,time,pathlib;subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[1],sys.argv[2]]);p=pathlib.Path(sys.argv[2]);\nwhile not p.exists():time.sleep(.01)"
  p,run=r.spawn_owned(self.reg,'capture',[sys.executable,'-c',leader,str(held),str(pidfile),grand],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   p.wait(timeout=3);pid=int(pidfile.read_text())
   c=scope['Controller']();c._assert_controller_thread=lambda:None;c.worker=p;c.capture_owner_run=run;c.capture_owner_released=False;c.camera_registry=self.reg;c.path=self.root;c.recording_store=Mock();c.state_write=Mock();c.last_help=[];c.discard_deadline=time.monotonic()+1
   c.poll_discard();c.recording_store.discard_current.assert_not_called();self.assertEqual(c.capture_owner_run,run);self.assertIs(c.worker,p);self.assertTrue(held.exists());self.assertIn('未确认',c.last_help[0])
  finally:
   if pidfile.exists():
    try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
    except ProcessLookupError:pass
   p.stdin.close()

 def test_live_owner_and_wrapper_never_clear_when_recorded_boot_changed(self):
  p,run=r.spawn_owned(self.reg,'scan',[sys.executable,'-c','import time;time.sleep(20)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   deadline=time.monotonic()+3
   while time.monotonic()<deadline:
    value=self.reg.inspect()
    if value and value.get('phase')=='registered':break
    time.sleep(.01)
   self.assertEqual(value['phase'],'registered')
   with self.reg.locked():
    value=self.reg._read();value['boot']='11111111-1111-4111-8111-111111111111';self.reg._write(value)
   with self.assertRaises(r.RegistryError):self.reg.reconcile()
   with self.assertRaises(r.RegistryError):self.reg.reserve('capture')
  finally:
   p.kill();p.wait(timeout=3)
 def test_member_wrong_group_and_wrong_run_block(self):
  self.reg.reserve('scan')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=123456789,wrapper_start=999,pgid=123456789,sid=123456789);self.reg._write(value)
  with patch.object(r,'proc_identity',return_value=dict(start=999,state='S',pgrp=123,sid=123)):
   with self.assertRaises(r.RegistryError):self.reg.verify_member(value['run'])
   with self.assertRaises(r.RegistryError):self.reg.verify_member('old-run')
 def test_alternate_profile_legacy_marker_blocks_canonical_registry(self):
  base=self.root/'orangepi';runtime=base/'runtime';runtime.mkdir(parents=True);registry=r.Registry(runtime)
  legacy=self.root/'profiles/eeg/orangepi/runtime/camera_owner.json';legacy.parent.mkdir(parents=True);legacy.write_text('unknown')
  with patch.object(r,'canonical_runtime',return_value=runtime.resolve()):
   with self.assertRaises(r.RegistryError):registry.reserve('scan')
  self.assertTrue(legacy.exists())
 def test_maintenance_diagnostic_refuses_live_controller_lock(self):
  import fcntl
  base=ROOT.parents[2];runtime=base/'runtime';runtime.mkdir(exist_ok=True);lock=runtime/'launcher.lock'
  with lock.open('a+') as held:
   fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
   script=base/'scripts/diagnostics/check_busy_device.py'
   result=subprocess.run([sys.executable,str(script)],capture_output=True,text=True,timeout=3)
   self.assertNotEqual(result.returncode,0);self.assertIn('采集服务正在运行',result.stderr)

 def test_production_wrapper_rejects_arbitrary_entry_before_execution(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('strict_registry',ROOT/'scan_registry.py');strict=importlib.util.module_from_spec(spec);spec.loader.exec_module(strict)
  registry=strict.Registry(strict.canonical_runtime());run=registry.reserve('scan');sentinel=self.root/'forbidden'
  try:
   command=[sys.executable,str(ROOT/'camera_owner_wrapper.py'),str(registry.root),run,sys.executable,'-c',"open(%r,'w').write('unsafe')"%str(sentinel)]
   result=subprocess.run(command,capture_output=True,text=True,start_new_session=True,timeout=3)
   self.assertNotEqual(result.returncode,0);self.assertFalse(sentinel.exists());self.assertEqual(registry.inspect()['phase'],'reserved')
  finally:registry.cancel_unspawned(run)
 def test_production_route_rejects_untrusted_probe(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('strict_registry',ROOT/'scan_registry.py');strict=importlib.util.module_from_spec(spec);spec.loader.exec_module(strict)
  with self.assertRaises(strict.RegistryError):strict.validate_owned_command('scan',[sys.executable,'-c','pass'])

 def test_allowlisted_routes_accept_both_profile_paths(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('strict_registry',ROOT/'scan_registry.py');strict=importlib.util.module_from_spec(spec);spec.loader.exec_module(strict)
  import project_paths
  with tempfile.TemporaryDirectory() as tmp:
   for profile in ('emg','eeg'):
    base=pathlib.Path(tmp)/'profiles'/profile/'orangepi';src=pathlib.Path(tmp)/'code/src/orangepi_collect';code=pathlib.Path(tmp)/'code'
    with patch.object(project_paths,'BASE',base),patch.object(project_paths,'SRC',src),patch.object(project_paths,'CODE_BASE',code):
     hardware=str(base/'.venvs/hardware/bin/python');framework=str(base/'.venvs/framework/bin/python')
     for item,version in ((hardware,'3.8'),(framework,'3.11')):
      link=pathlib.Path(item);link.parent.mkdir(parents=True,exist_ok=True);link.symlink_to('/tmp/python'+version)
     routes={'scan':[hardware,str(src/'acquisition/scan_devices.py')], 'preview':[hardware,'-u',str(src/'acquisition/preview_worker.py')], 'calibration':[hardware,str(src/'acquisition/calibration_probe.py')], 'settings':[hardware,str(src/'control/wrist_settings_worker.py')], 'capture':[framework,'-u',str(src/'framework/framework_recorder.py'),'--output','/tmp/session','--wait-for-save'], 'diagnostic':[hardware,str(code/'scripts/diagnostics/check_busy_device.py'),'--registered-child']}
     for kind,command in routes.items():strict.validate_owned_command(kind,command)
     wrong=base/'.venvs/other/bin/python';wrong.parent.mkdir(parents=True,exist_ok=True);wrong.symlink_to('/tmp/python3.8')
     with self.assertRaises(strict.RegistryError):strict.validate_owned_command('scan',[str(wrong),str(src/'acquisition/scan_devices.py')])

 def test_same_owner_retries_terminal_cleanup_after_witness_deleted(self):
  p,run=r.spawn_owned(self.reg,'scan',[sys.executable,'-c','pass'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);p.wait(timeout=3)
  def partial():self.reg.witness.unlink();raise OSError('interrupted cleanup')
  with patch.object(self.reg,'_remove_active',side_effect=partial):
   with self.assertRaises(OSError):self.reg.finish(run)
  self.assertFalse(self.reg.witness.exists());self.assertEqual(self.reg.inspect()['phase'],'terminal')
  self.reg.finish(run);self.assertIsNone(self.reg.inspect())

if __name__=='__main__':unittest.main(verbosity=2)
