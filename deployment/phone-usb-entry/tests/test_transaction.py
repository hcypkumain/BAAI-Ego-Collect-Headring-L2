import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_deploy',HERE/'deploy_bundle.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)

def sha(b):return hashlib.sha256(b).hexdigest()

class TransactionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.state=self.root/'state';self.units=self.root/'units';self.units.mkdir();self.collector=self.root/'collector';(self.collector/'src').mkdir(parents=True)
  self.target=self.collector/'src/foo.py';self.target.write_bytes(b'old')
  self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
  self.stack.enter_context(patch.object(d,'STATE',self.state));self.stack.enter_context(patch.object(d,'UNITS',self.units));self.stack.enter_context(patch.object(d.os,'fchown',lambda *_:None))
  original_stat=Path.stat
  def test_stat(path,*args,**kwargs):
   result=original_stat(path,*args,**kwargs)
   if path==self.state:
    values=list(result);values[4]=0;return os.stat_result(values)
   return result
  self.stack.enter_context(patch.object(Path,'stat',test_stat))
 def test_failure_restores_files_under_lease(self):
  m={'release_id':'offline-0123456789abcdef','release_prefix':str(self.root/'release'),'files':{'release/a':{'mode':0o644}}}
  files={'collector/orangepi/src/foo.py':b'new','release/a':b'release','support/services/usb-phone-link/usb_data_host.py':b'fixture'}
  plan={'collector_changes':['src/foo.py'],'collector_prehash':{'src/foo.py':sha(b'old')},'legacy_units':{},'release_prefix':m['release_prefix']}
  lease=[False];events=[]
  @contextlib.contextmanager
  def capture(*_):
   lease[0]=True
   try:yield
   finally:lease[0]=False
  def run(args,*_a,**_kw):
   self.assertTrue(lease[0]);events.append(args[1:]);return None
  real=d.backup_and_write
  def fail(target,data,mode,j,tx):
   if str(target).endswith('/release/a'):raise OSError('injected')
   return real(target,data,mode,j,tx)
  with patch.object(d,'verify_environment',return_value=plan),patch.object(d,'acquire_capture_guard',capture),patch.object(d,'cmd',side_effect=run),patch.object(d,'backup_and_write',side_effect=fail):
   with self.assertRaises(OSError):d.apply(m,files,self.collector,plan)
  self.assertEqual(self.target.read_bytes(),b'old');self.assertEqual(json.loads((self.state/'transaction-offline-0123456789abcdef/journal.json').read_text())['status'],'rolled_back');self.assertFalse(lease[0]);self.assertTrue(any('stop' in x for x in events));self.assertTrue(any('start' in x for x in events))
 def test_changed_target_and_bad_backup_refuse_before_service_action(self):
  tx=self.root/'tx';tx.mkdir();backup=tx/'old';backup.write_bytes(b'bad');self.target.write_bytes(b'new')
  j={'files':[{'target':str(self.target),'post_sha':sha(b'new'),'existed':True,'old_sha':sha(b'old'),'backup':str(backup),'mode':0o644,'uid':os.getuid(),'gid':os.getgid()}],'legacy_units':{},'collector_stop_intent':False}
  with patch.object(d,'cmd') as command:
   with self.assertRaises(RuntimeError):d.rollback(tx,j)
   command.assert_not_called()
  self.assertEqual(self.target.read_bytes(),b'new')
  backup.write_bytes(b'old');self.target.write_bytes(b'user-change')
  with patch.object(d,'cmd') as command:
   with self.assertRaises(RuntimeError):d.rollback(tx,j)
   command.assert_not_called()
  self.assertEqual(self.target.read_bytes(),b'user-change')
 def test_idle_rollback_stops_before_restore_and_starts_after(self):
  tx=self.root/'tx';tx.mkdir();backup=tx/'old';backup.write_bytes(b'old');self.target.write_bytes(b'new');events=[]
  j={'files':[{'target':str(self.target),'post_sha':sha(b'new'),'existed':True,'old_sha':sha(b'old'),'backup':str(backup),'mode':0o644,'uid':os.getuid(),'gid':os.getgid()}],'legacy_units':{},'collector_stop_intent':True}
  def command(args,*_a,**_k):
   if args[1]=='stop':self.assertEqual(self.target.read_bytes(),b'new')
   if args[1]=='start':self.assertEqual(self.target.read_bytes(),b'old')
   events.append(args[1]);return None
  with patch.object(d,'cmd',side_effect=command):d.rollback(tx,j)
  self.assertEqual(events,['stop','daemon-reload','start']);self.assertEqual(j['status'],'rolled_back')
 def test_unchanged_apply_is_noop(self):
  with patch.object(d,'load_bundle',return_value=({'device_id':'cp_0021'},{})),patch.object(d,'verify_environment',return_value={'already':True,'collector_changes':[],'legacy_units':{},'release_prefix':'/test'}),patch.object(d,'apply') as apply,patch('sys.argv',['deploy','apply','--bundle','/test','--sha256','a']):
   d.main();apply.assert_not_called()
 def test_failed_owned_upgrade_restores_old_units_under_lease(self):
  m={'release_id':'offline-0123456789abcdef','release_prefix':str(self.root/'release'),'files':{'release/a':{'mode':0o644}}}
  files={'release/a':b'release','support/services/usb-phone-link/usb_data_host.py':b'fixture'}
  previous={name:{'active':True,'enabled':True} for name in d.SERVICES}
  for name in d.SERVICES:(self.units/name).write_bytes(('old-'+name).encode())
  plan={'collector_changes':[],'collector_prehash':{},'legacy_units':{},'previous_release':{'release_id':'offline-fedcba9876543210','journal_sha256':'0'*64,'units':previous},'release_prefix':m['release_prefix']}
  lease=[False];events=[]
  @contextlib.contextmanager
  def capture(*_):
   lease[0]=True
   try:yield
   finally:lease[0]=False
  def command(args,*_a,**_kw):
   self.assertTrue(lease[0]);events.append(args[1:]);return None
  real=d.backup_and_write
  def inject(target,data,mode,j,tx):
   if target==self.units/d.SERVICES[1]:raise OSError('injected unit failure')
   return real(target,data,mode,j,tx)
  account=type('Account',(),{'pw_uid':123,'pw_gid':123,'pw_shell':'/usr/sbin/nologin'})();group=type('Group',(),{'gr_gid':123})()
  with patch.object(d,'verify_environment',return_value=plan),patch.object(d,'acquire_capture_guard',capture),patch.object(d,'cmd',side_effect=command),patch.object(d,'backup_and_write',side_effect=inject),patch.object(d.pwd,'getpwnam',return_value=account),patch.object(d.grp,'getgrnam',return_value=group):
   with self.assertRaises(OSError):d.apply(m,files,self.collector,plan)
  for name in d.SERVICES:
   self.assertEqual((self.units/name).read_bytes(),('old-'+name).encode())
   self.assertIn(['enable',name],events);self.assertIn(['start',name],events)
  self.assertFalse(lease[0]);self.assertEqual(json.loads((self.state/'transaction-offline-0123456789abcdef/journal.json').read_text())['status'],'rolled_back')
 def test_new_collector_source_removed_on_failed_install(self):
  missing=self.collector/'src/added.py';self.assertFalse(missing.exists())
  m={'release_id':'offline-0123456789abcdef','release_prefix':str(self.root/'release'),'files':{'release/a':{'mode':0o644}}}
  files={'collector/orangepi/src/added.py':b'new-source','release/a':b'release','support/services/usb-phone-link/usb_data_host.py':b'fixture'}
  plan={'collector_changes':['src/added.py'],'collector_prehash':{'src/added.py':None},'legacy_units':{},'release_prefix':m['release_prefix']}
  lease=[False]
  @contextlib.contextmanager
  def capture(*_):
   lease[0]=True
   try:yield
   finally:lease[0]=False
  def command(args,*_a,**_k):self.assertTrue(lease[0]);return None
  real=d.backup_and_write
  def fail(target,data,mode,j,tx):
   if str(target).endswith('/release/a'):self.assertTrue(missing.exists());raise OSError('injected')
   return real(target,data,mode,j,tx)
  with patch.object(d,'verify_environment',return_value=plan),patch.object(d,'acquire_capture_guard',capture),patch.object(d,'cmd',side_effect=command),patch.object(d,'backup_and_write',side_effect=fail):
   with self.assertRaises(OSError):d.apply(m,files,self.collector,plan)
  self.assertFalse(missing.exists());self.assertFalse(lease[0])
 def test_busy_explicit_rollback_has_no_writes(self):
  tx=self.state/'transaction-offline-0123456789abcdef';tx.mkdir(parents=True);guard=tx/'capture_guard.py';guard.write_bytes(b'guard')
  (tx/'journal.json').write_text(json.dumps({'status':'complete','capture_guard_sha':sha(b'guard'),'collector_root':str(self.collector),'collector_changed':True,'legacy_units':{}}))
  @contextlib.contextmanager
  def busy(*_):
   raise RuntimeError('busy')
   yield
  with patch.object(d.os,'geteuid',return_value=0),patch.object(d,'acquire_capture_guard',busy),patch.object(d,'rollback') as rollback,patch('sys.argv',['deploy','rollback','--transaction',str(tx)]):
   with self.assertRaises(RuntimeError):d.main()
   rollback.assert_not_called()
  self.assertEqual(self.target.read_bytes(),b'old')
if __name__=='__main__':unittest.main()
