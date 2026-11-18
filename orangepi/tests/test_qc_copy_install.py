import hashlib,importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2];spec=importlib.util.spec_from_file_location('copy_installer',ROOT/'deployment/host-qc-copy-only/install_copy_only.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class InstallTest(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.r=Path(t.name).resolve();self.root=self.r/'live';self.stage=self.r/'stage';self.root.mkdir();self.stage.mkdir();self.state=self.r/'state';self.state.mkdir();self.db=self.state/'receiver.sqlite3'
  c=sqlite3.connect(self.db);c.executescript('CREATE TABLE control(id INTEGER PRIMARY KEY, receiving INTEGER,revision INTEGER);INSERT INTO control VALUES(1,0,2); CREATE TABLE jobs(id INTEGER PRIMARY KEY);');c.close()
  (self.root/'config.json').write_text(json.dumps({'receive_strategy':'manual_handoff_v1','receiver_state_root':str(self.state)}))
  self.before={n:(('# before '+n+'\n') if n.endswith('.py') else 'before').encode() for n in m.FILES};self.after={n:(('# after '+n+'\n') if n.endswith('.py') else 'after').encode() for n in m.FILES}
  for n in m.FILES:(self.root/n).write_bytes(self.before[n]);(self.stage/n).write_bytes(self.after[n])
  value={'schema':1,'version':'20261119-copy-only-v1','files':{n:{'before':m.sha(self.before[n]),'after':m.sha(self.after[n])} for n in m.FILES}};self.manifest=json.dumps(value,sort_keys=True,separators=(',',':')).encode();(self.stage/'manifest.json').write_bytes(self.manifest);self.hash=m.sha(self.manifest)
  self.pid=[111];self.unit=lambda **kw:(self.pid[0],'/system.slice/headring-qc.service');self.sig=Mock(side_effect=lambda _:self.pid.__setitem__(0,self.pid[0]+111));self.status=Mock(return_value={})
 def patches(self):
  return patch.multiple(m,ROOT=self.root,unit=self.unit,guarded_signal=self.sig,read_status_stopped=self.status)
 def test_success_keeps_database_and_saves_preimages(self):
  with self.patches():m.install(self.stage,self.hash)
  for n in m.FILES:self.assertEqual((self.root/n).read_bytes(),self.after[n])
  c=sqlite3.connect(self.db);self.assertEqual(c.execute('select receiving,revision from control').fetchone(),(0,2));c.close()
  b=next((self.root/'backups').iterdir());self.assertEqual((b/'service.py').read_bytes(),self.before['service.py']);self.assertTrue((b/'receiver-evidence.sqlite3').is_file());self.sig.assert_called_once()
 def test_mid_switch_failure_restores_sources_without_signal(self):
  real=m.atomic
  def inject(path,data,mode):
   if Path(path)==self.root/'handoff_receiver.py' and data==self.after['handoff_receiver.py']:raise OSError('injected')
   return real(path,data,mode)
  with self.patches(),patch.object(m,'atomic',side_effect=inject):
   with self.assertRaises(OSError):m.install(self.stage,self.hash)
  for n in m.FILES:self.assertEqual((self.root/n).read_bytes(),self.before[n])
  self.sig.assert_not_called()
 def test_replace_succeeded_but_parent_fsync_failed_restores_preimage(self):
  real=m.sync_dir;fired=[False]
  def inject(path):
   if Path(path)==self.root and (self.root/'service.py').read_bytes()==self.after['service.py'] and not fired[0]:
    fired[0]=True;raise OSError('injected parent fsync failure')
   return real(path)
  with self.patches(),patch.object(m,'sync_dir',side_effect=inject):
   with self.assertRaisesRegex(OSError,'fsync'):m.install(self.stage,self.hash)
  self.assertTrue(fired[0]);self.assertEqual((self.root/'service.py').read_bytes(),self.before['service.py']);self.sig.assert_not_called()

 def test_unhealthy_new_service_restores_programs_only(self):
  self.status.side_effect=lambda version=None: (_ for _ in ()).throw(ValueError('bad version')) if version not in (None,'legacy') else {}
  with self.patches(),patch.object(m.time,'sleep',lambda _:None):
   with self.assertRaises(RuntimeError):m.install(self.stage,self.hash)
  for n in m.FILES:self.assertEqual((self.root/n).read_bytes(),self.before[n])
  c=sqlite3.connect(self.db);self.assertEqual(c.execute('select receiving,revision from control').fetchone(),(0,2));c.close();self.assertEqual(self.sig.call_count,2)
 def test_rollback_restores_files_when_service_temporarily_has_no_pid(self):
  self.status.side_effect=lambda version=None: (_ for _ in ()).throw(ValueError('bad version')) if version not in (None,'legacy') else {}
  state={'rollback':False}
  def transient(**kw):
   if kw.get('allow_inactive') and not state['rollback']:
    state['rollback']=True;self.pid[0]=333;return 0,'/system.slice/headring-qc.service'
   return self.pid[0],'/system.slice/headring-qc.service'
  self.unit=transient
  with self.patches(),patch.object(m.time,'sleep',lambda _:None):
   with self.assertRaises(RuntimeError):m.install(self.stage,self.hash)
  for n in m.FILES:self.assertEqual((self.root/n).read_bytes(),self.before[n])
  self.assertEqual(self.sig.call_count,1)

 def test_new_job_blocks_all_mutation(self):
  c=sqlite3.connect(self.db);c.execute('insert into jobs values(1)');c.commit();c.close()
  with self.patches():
   with self.assertRaisesRegex(RuntimeError,'zero jobs'):m.install(self.stage,self.hash)
  for n in m.FILES:self.assertEqual((self.root/n).read_bytes(),self.before[n])
  self.sig.assert_not_called()
if __name__=='__main__':unittest.main()
