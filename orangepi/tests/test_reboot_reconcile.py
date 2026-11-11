"""Explicit witness-backed maintenance recovery; no real reboot or devices."""
import fcntl,json,os,pathlib,signal,subprocess,sys,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
sys.path.insert(0,str(ROOT))
import scan_registry as r
NEWBOOT='11111111-1111-4111-8111-111111111111'
NEXTBOOT='22222222-2222-4222-8222-222222222222'

class RebootRecovery(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name);self.reg=r.Registry(self.root)
 def recover(self):
  # Simulate independently read new kernel boot and absence of old processes.
  with patch.object(r,'boot_id',return_value=NEWBOOT),patch.object(r,'proc_identity',return_value=None),patch.object(r,'group_exists',return_value=False):
   witness=self.reg._witness() if self.reg.witness.exists() else None
   if witness:run=witness['run']
   else:
    record=self.reg._read();run=record['run']
   return self.reg.reconcile_after_reboot(run)
 def startup(self,lock):return self.reg.reconcile_startup(lock)
 def test_witness_only_then_explicit_reboot_recovery(self):
  with patch.object(self.reg,'_write',side_effect=OSError('record write failure')):
   with self.assertRaises(OSError):self.reg.reserve('scan')
  self.assertTrue(self.reg.witness.exists());self.assertFalse(self.reg.record.exists())
  self.assertIsNotNone(self.reg.inspect_evidence()['witness']['run'])
  with self.assertRaises(r.RegistryError):self.reg.reserve('preview')
  result=self.recover();self.assertTrue(pathlib.Path(result['receipt']).exists());self.assertFalse(self.reg.witness.exists());self.assertFalse(self.reg.record.exists())
 def test_dead_controller_reserved_recovered_only_with_new_boot(self):
  code="import sys,signal;sys.path.insert(0,sys.argv[1]);from scan_registry import Registry;r=Registry(sys.argv[2]);r.reserve('scan');print('reserved',flush=True);signal.pause()"
  proc=subprocess.Popen([sys.executable,'-c',code,str(ROOT),str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  self.assertEqual(proc.stdout.readline().strip(),'reserved');proc.kill();proc.wait();proc.stdout.close();proc.stderr.close()
  with self.assertRaises(r.RegistryError):self.reg.reconcile_after_reboot(self.reg.inspect()['run'])
  result=self.recover();receipt=self.reg._read_checked(pathlib.Path(result['receipt']))
  self.assertEqual(receipt['run'],result['run']);self.assertEqual(receipt['new_boot'],NEWBOOT);self.assertTrue((self.root/'camera_owner_audit'/(result['run']+'.registry.raw')).exists())
 def test_boot_unreadable_blocks_recovery(self):
  self.reg.reserve('scan')
  with patch.object(r,'boot_id',return_value=None):
   with self.assertRaises(r.RegistryError):self.reg.reconcile_after_reboot(self.reg.inspect()['run'])
  self.assertTrue(self.reg.record.exists())
 def test_maintenance_lock_busy_blocks(self):
  self.reg.reserve('scan')
  with (self.root/'launcher.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaises(r.RegistryError):self.recover()
 def test_corrupt_witness_or_mismatched_run_nonce_block(self):
  self.reg.reserve('scan');original=self.reg.witness.read_bytes();value=json.loads(original);value['nonce']='changed';self.reg.witness.write_text(json.dumps(value))
  with self.assertRaises(r.RegistryError):self.recover()
  self.reg.witness.write_bytes(original);record=json.loads(self.reg.record.read_text());record['run']='wrong-run';self.reg.record.write_text(json.dumps(record))
  with self.assertRaises(r.RegistryError):self.recover()
 def test_corrupt_record_with_run_nonce_bound_is_archived(self):
  self.reg.reserve('scan');record=json.loads(self.reg.record.read_text());record['boot']='broken';self.reg.record.write_text(json.dumps(record))
  result=self.recover();archive=self.root/'camera_owner_audit'/(result['run']+'.registry.raw');self.assertEqual(json.loads(archive.read_text())['boot'],'broken')
 def test_receipt_and_terminal_interruption_are_idempotent(self):
  for stage in ('receipt','terminal','after_witness_delete'):
   with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
    self.reg=r.Registry(tmp);self.reg.reserve('scan')
    if stage=='receipt':patcher=patch.object(self.reg,'_write',side_effect=OSError('stop after receipt'))
    elif stage=='terminal':patcher=patch.object(self.reg,'_remove_active',side_effect=OSError('stop after terminal'))
    else:
     def partial():self.reg.witness.unlink();raise OSError('stop after witness deletion')
     patcher=patch.object(self.reg,'_remove_active',side_effect=partial)
    with patcher:
     with self.assertRaises(OSError):self.recover()
    result=self.recover();self.assertTrue(pathlib.Path(result['receipt']).exists());self.assertFalse(self.reg.record.exists());self.assertFalse(self.reg.witness.exists())
 def test_completed_maintenance_retry_with_exact_run_is_idempotent(self):
  run=self.reg.reserve('scan');result=self.recover()
  with patch.object(r,'boot_id',return_value=NEWBOOT):again=self.reg.reconcile_after_reboot(run)
  self.assertEqual(result['run'],again['run']);self.assertTrue(again['already_terminal'])
  with patch.object(r,'boot_id',return_value=NEWBOOT):
   with self.assertRaises(r.RegistryError):self.reg.reconcile_after_reboot('0'*32)

 def test_startup_recovers_old_registered_run_after_finish_write_failure(self):
  run=self.reg.reserve('capture');session=self.root/'session-data.bin';session.write_bytes(b'original capture bytes')
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=987654321,wrapper_start=987,
    pgid=987654321,sid=987654321);self.reg._write(value)
  with patch.object(r,'group_exists',return_value=False),patch.object(self.reg,'_write',side_effect=OSError('finish fsync failed')):
   with self.assertRaises(OSError):self.reg.finish(run)
  self.assertEqual(self.reg.inspect()['phase'],'registered')
  with (self.root/'launcher.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with patch.object(r,'boot_id',return_value=NEWBOOT),patch.object(r,'proc_identity',return_value=None),patch.object(r,'group_exists',return_value=False):
    result=self.startup(lock)
  self.assertEqual(result['run'],run);self.assertFalse(self.reg.record.exists());self.assertFalse(self.reg.witness.exists())
  self.assertEqual(session.read_bytes(),b'original capture bytes')
  receipt=self.reg._read_checked(pathlib.Path(result['receipt']))
  self.assertEqual(receipt['terminal_reason'],'after_reboot');self.assertFalse(receipt['successful'])
  raw=json.loads((self.root/'camera_owner_audit'/(run+'.registry.raw')).read_text())
  self.assertEqual(raw['run'],run);self.assertEqual(raw['kind'],'capture');self.assertEqual(raw['phase'],'registered')
  # A fresh operation may enter the normal device-confirmation path.
  self.assertEqual(self.reg.inspect(),None);self.assertNotEqual(self.reg.reserve('scan'),run)

 def test_startup_prior_boot_does_not_confuse_reused_owner_pid_or_pgid(self):
  run=self.reg.reserve('scan');owner=self.reg.inspect()
  with self.reg.locked():
   value=self.reg._read();value.update(phase='registered',wrapper_pid=owner['owner_pid'],
    wrapper_start=owner['owner_start'],pgid=owner['owner_pid'],sid=owner['owner_pid']);self.reg._write(value)
  with (self.root/'launcher.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with patch.object(r,'boot_id',return_value=NEWBOOT),patch.object(r,'proc_identity',return_value=dict(start=owner['owner_start'],state='S')) as identity,patch.object(r,'group_exists',return_value=True) as group:
    result=self.startup(lock)
  self.assertEqual(result['run'],run);self.assertFalse(self.reg.record.exists());identity.assert_not_called();group.assert_not_called()

 def test_startup_same_boot_keeps_live_owner_locked(self):
  run=self.reg.reserve('scan')
  with (self.root/'launcher.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   result=self.startup(lock)
  self.assertEqual(result,dict(run=run,boot=self.reg.inspect()['boot'],preserved=True))
  self.assertEqual(self.reg.inspect()['run'],run);self.assertTrue(self.reg.witness.exists())

 def test_startup_never_recovers_witness_missing_controller_start_identity(self):
  self.reg.reserve('scan');witness=json.loads(self.reg.witness.read_text());witness.pop('integrity_sha256');witness.pop('owner_start')
  witness['integrity_sha256']=r.checksum(witness);self.reg.witness.write_text(json.dumps(witness))
  with (self.root/'launcher.lock').open('a+') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with patch.object(r,'boot_id',return_value=NEWBOOT):
    with self.assertRaises(r.RegistryError):self.startup(lock)
  self.assertTrue(self.reg.record.exists());self.assertTrue(self.reg.witness.exists())

 def test_interrupted_cleanup_recovers_after_another_host_reboot(self):
  run=self.reg.reserve('capture')
  with patch.object(r,'boot_id',return_value=NEWBOOT),patch.object(r,'proc_identity',return_value=None),patch.object(r,'group_exists',return_value=False),patch.object(self.reg,'_write',side_effect=OSError('host reboot before terminal write')):
   with self.assertRaises(OSError):self.reg.reconcile_after_reboot(run)
  receipt_path=self.root/'camera_owner_audit'/(run+'.json')
  self.assertTrue(receipt_path.exists());self.assertTrue(self.reg.record.exists())
  with patch.object(r,'boot_id',return_value=NEXTBOOT),patch.object(r,'proc_identity',return_value=None),patch.object(r,'group_exists',return_value=False):
   result=self.reg.reconcile_after_reboot(run)
  self.assertEqual(result['run'],run);self.assertFalse(self.reg.record.exists());self.assertFalse(self.reg.witness.exists())
  self.assertEqual(self.reg._read_checked(receipt_path)['new_boot'],NEWBOOT)

 def test_real_controller_dies_after_witness_before_registry(self):
  code="import sys,os,signal;sys.path.insert(0,sys.argv[1]);from scan_registry import Registry;r=Registry(sys.argv[2]);r._write=lambda value:os.kill(os.getpid(),signal.SIGKILL);r.reserve('scan')"
  proc=subprocess.run([sys.executable,'-c',code,str(ROOT),str(self.root)],capture_output=True,timeout=3)
  self.assertNotEqual(proc.returncode,0);self.assertTrue(self.reg.witness.exists());self.assertFalse(self.reg.record.exists())
  with self.assertRaises(r.RegistryError):self.reg.reserve('scan')
  self.assertTrue(pathlib.Path(self.recover()['receipt']).exists())
 def test_old_receipt_retry_cannot_clear_new_active_run(self):
  old=self.reg.reserve('scan');self.recover();new=self.reg.reserve('preview')
  with patch.object(r,'boot_id',return_value=NEWBOOT):
   with self.assertRaises(r.RegistryError):self.reg.reconcile_after_reboot(old)
  self.assertEqual(self.reg.inspect()['run'],new)
 def test_corrupt_receipt_cannot_fake_already_completed_maintenance(self):
  run=self.reg.reserve('scan');result=self.recover();pathlib.Path(result['receipt']).write_text('{}')
  with patch.object(r,'boot_id',return_value=NEWBOOT):
   with self.assertRaises(r.RegistryError):self.reg.reconcile_after_reboot(run)

if __name__=='__main__':unittest.main(verbosity=2)
