import importlib.util,json,shutil,sys,tempfile,threading,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'deployment/host-qc-copy-only'))
from receiver_state import ReceiverStore,Conflict
from handoff_receiver import HandoffReceiver,Paused,COPY_PURPOSE
from raw_handoff import canonical,sha_value,verify_signature,validate_raw_receipt
import hashlib

class CopyOnlyTest(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name).resolve();self.data=self.root/'data';(self.data/'originals').mkdir(parents=True);self.device='cp_0017';self.sid='L2-copy-test';self.day='2026-11-19';self.source=self.root/'pi-source'/self.sid;self.source.mkdir(parents=True)
  (self.source/'native_capture_summary.json').write_text('{"status":"success"}');(self.source/'stream.bin').write_bytes(b'captured-data')
  rows=[{'path':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(self.source.iterdir())]
  self.manifest={'device_id':self.device,'session_id':self.sid,'root':str(self.source.parent),'files':rows,'total_files':len(rows),'total_bytes':sum(x['bytes'] for x in rows),'digest':sha_value(rows),'native_sha256':rows[0]['sha256']}
  (self.root/'secrets').mkdir();(self.root/'secrets'/self.device).write_bytes(b'test-key')
  self.store=ReceiverStore(self.root/'receiver');self.ident=self.store.enqueue(self.device,{'session_id':self.sid,'day':self.day,'request_id':'request','native_sha256':self.manifest['native_sha256'],'path':str(self.source)})
  self.control=self.store.set_control(True,0,uuid.uuid4().hex)
  self.actor=HandoffReceiver.__new__(HandoffReceiver);self.actor.root=self.root;self.actor.data=self.data;self.actor.config={'server_id':'server','storage_uuid':'disk','copy_only_min_free_bytes':0};self.actor.store=self.store;self.actor.pi_locks={self.device:threading.RLock()};self.actor.wake=SimpleNamespace(set=Mock());self.actor._storage=Mock();self.actor._idle=Mock()
  self.actor.legacy=SimpleNamespace(verify_tree=self.verify_tree,sync_dir=lambda _:None)
  self.actor.rpc=Mock(side_effect=lambda d,action,**kw: self.manifest if action=='manifest' else (_ for _ in ()).throw(AssertionError('unexpected RPC '+action)))
  self.actor._transfer=lambda job,target:shutil.copytree(self.source,target,dirs_exist_ok=True)
 def verify_tree(self,root,manifest,durable=False):
  self.assertTrue(durable);self.assertEqual({p.name for p in Path(root).iterdir()},set(x['path'] for x in manifest['files']))
  for row in manifest['files']:self.assertEqual(hashlib.sha256((Path(root)/row['path']).read_bytes()).hexdigest(),row['sha256'])
 def test_copy_is_durable_held_and_not_a_delete_receipt(self):
  before={p.name:p.read_bytes() for p in self.source.iterdir()};self.actor.receive(self.store.job(self.ident));job=self.store.job(self.ident)
  self.assertEqual(job['receive_state'],'host_committed');self.assertEqual(job['release_state'],'hold_copy_only');self.assertEqual(job['process_state'],'hold_copy_only');self.assertTrue(job['retention_hold']);self.assertNotIn('raw_receipt',job);self.assertNotIn('release_decision_at',job)
  verify_signature(job['copy_receipt'],b'test-key',COPY_PURPOSE)
  with self.assertRaises(ValueError):validate_raw_receipt(job['copy_receipt'],b'test-key',self.device,'server',self.source.parent)
  self.assertEqual(before,{p.name:p.read_bytes() for p in self.source.iterdir()})
  for stage in ('release','process','reply'):self.assertIsNone(self.store.claim(stage))
  with self.assertRaises(ValueError):self.store.authorize_release(self.ident,self.control['revision'])
  self.actor.receive(job);self.assertEqual(self.store.job(self.ident)['copy_receipt'],job['copy_receipt'])
 def test_stop_wins_before_receipt_commit(self):
  def stop_during_transfer(job,target):
   shutil.copytree(self.source,target,dirs_exist_ok=True);self.store.set_control(False,self.control['revision'],uuid.uuid4().hex)
  self.actor._transfer=stop_during_transfer
  with self.assertRaises(Conflict):self.actor.receive(self.store.job(self.ident))
  job=self.store.job(self.ident);self.assertNotIn('copy_receipt',job);self.assertNotIn('raw_receipt',job);self.assertTrue(self.source.exists());self.assertIsNone(self.store.claim('release'))
 def test_direct_mutation_paths_are_blocked(self):
  for action in ('raw_handoff','cleanup','apply','apply_handoff_result','prepare_handoff_results'):
   with self.subTest(action=action),self.assertRaises(Paused):HandoffReceiver.rpc(self.actor,self.device,action)
  for method in ('release','process','reply'):
   with self.subTest(method=method),self.assertRaises(Paused):getattr(self.actor,method)({})
 def test_old_receiver_cannot_claim_copy_only_job_after_rollback(self):
  self.actor.receive(self.store.job(self.ident))
  oldpath=ROOT.parent/'qc-frozen-old/receiver_state.py';spec=importlib.util.spec_from_file_location('old_receiver_fixture',oldpath);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);old=m.ReceiverStore(self.root/'receiver')
  for stage in ('release','process','reply'):self.assertIsNone(old.claim(stage))
 def test_interrupted_copy_is_not_claimed_by_old_receiver(self):
  running=self.store.claim('receive');self.assertEqual(running['receive_state'],'copy_only_running')
  oldpath=ROOT.parent/'qc-frozen-old/receiver_state.py';spec=importlib.util.spec_from_file_location('old_receiver_interrupted',oldpath);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);old=m.ReceiverStore(self.root/'receiver')
  self.assertEqual(old.job(self.ident)['receive_state'],'copy_only_running');self.assertIsNone(old.claim('receive'));self.assertIsNone(old.claim('release'))
  self.assertEqual(old.enqueue(self.device,{'session_id':self.sid,'native_sha256':self.manifest['native_sha256']}),self.ident);self.assertIsNone(old.claim('receive'))
  recovered=ReceiverStore(self.root/'receiver');self.assertEqual(recovered.job(self.ident)['receive_state'],'copy_only_retry')
 def test_resuming_existing_copy_rechecks_live_source_manifest(self):
  raw=self.data/'originals'/self.device/'received'/self.day/self.sid;raw.parent.mkdir(parents=True);shutil.copytree(self.source,raw)
  self.store.update(self.ident,manifest=self.manifest)
  changed=dict(self.manifest);changed['files']=[dict(x) for x in self.manifest['files']];changed['files'][1]['sha256']='0'*64;changed['digest']=sha_value(changed['files'])
  self.actor.rpc=Mock(return_value=changed)
  with self.assertRaisesRegex(ValueError,'Source changed before copy commit'):self.actor.receive(self.store.job(self.ident))
  self.assertNotIn('copy_receipt',self.store.job(self.ident));self.assertTrue(raw.exists());self.assertTrue(self.source.exists())

 def test_first_copy_fsyncs_created_ancestors_and_both_rename_parents(self):
  self.actor.legacy.sync_dir=Mock();self.actor.receive(self.store.job(self.ident))
  syncs={str(c.args[0]) for c in self.actor.legacy.sync_dir.call_args_list}
  originals=self.data/'originals';parent=originals/self.device/'received'/self.day
  for path in (originals,originals/self.device,originals/self.device/'received',parent,self.data/'incoming/manual-handoff'/self.device):
   self.assertIn(str(path),syncs)

 def test_insufficient_space_refuses_before_transfer(self):
  with patch('handoff_receiver.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
   with self.assertRaisesRegex(Paused,'Insufficient'):self.actor.receive(self.store.job(self.ident))
  self.assertTrue(self.source.exists());self.assertNotIn('copy_receipt',self.store.job(self.ident))

 def test_status_reports_copy_only_identity(self):
  status=self.actor.snapshot();self.assertIs(status['copy_only'],True);self.assertEqual(status['copy_only_version'],'20261119-copy-only-v1')

 def test_start_only_spawns_scanner_and_receive_workers(self):
  calls=[]
  class FakeThread:
   def __init__(self,*args,**kwargs):calls.append(kwargs)
   def start(self):pass
  with patch('handoff_receiver.threading.Thread',FakeThread):self.actor.start()
  self.assertEqual([v['name'] for v in calls],['manual-scanner','manual-receive'])
 def test_start_refuses_existing_handoff_transaction(self):
  self.store.update(self.ident,raw_receipt={'unsafe':True})
  with patch('handoff_receiver.threading.Thread') as threads:
   with self.assertRaises(Paused):self.actor.start()
   threads.assert_not_called()
if __name__=='__main__':unittest.main()
