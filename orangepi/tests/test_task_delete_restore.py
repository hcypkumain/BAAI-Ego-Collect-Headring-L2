import json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
p=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control';sys.path.insert(0,str(p))
from task_delete_restore import restore_saved_reference
class Ledger:
 def __init__(self,attempts,used=None):self.attempts=attempts;self.used=used
 def read(self,path):return {'used':{'a.png':self.used} if self.used else {},'attempts':{'a.png':self.attempts}}
class T(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.project=Path(self.t.name);self.base=self.project/'orangepi';(self.base/'runtime').mkdir(parents=True);self.rel='configs/environments/scene/a.png';p=self.project/self.rel;p.parent.mkdir(parents=True);p.write_bytes(b'x');self.session='/take/a';self.own={'session':self.session,'status':'pending_qc','has_data':True,'deleted':True,'retained':False};self.path=self.base/'runtime/reference_task_blocks.json';self.value={'version':4,'current_task':'task','target_size':5,'pending':None,'selected_ids':['img'],'saved_ids':['img'],'completed_ids':[],'saved_history':['img'],'block_saved_start':None,'finished_tasks':[],'finished_images':{},'image_refs':{'img':{'task':'task','relative_path':self.rel}}};self.write()
 def write(self):self.path.write_text(json.dumps(self.value))
 def runrestore(self,ledger=None):return restore_saved_reference(self.base,self.project,ledger or Ledger([self.own]),self.rel,self.session)
 def test_current_block_restore_and_replay(self):
  self.assertEqual(self.runrestore()['status'],'restored');v=json.loads(self.path.read_text());self.assertEqual(v['saved_ids'],[]);self.assertEqual(v['saved_history'],[]);self.assertEqual(v['selected_ids'],[]);self.assertEqual(self.runrestore()['status'],'restored');self.assertEqual(v,json.loads(self.path.read_text()))
 def test_another_take_or_used_owner_prevents_reduction(self):
  for ledger in [Ledger([self.own,{'session':'/take/other','status':'success','deleted':False}]),Ledger([self.own],{'session':'/take/other'})]:
   self.assertEqual(self.runrestore(ledger)['status'],'manual');self.assertEqual(json.loads(self.path.read_text()),self.value)
 def test_finished_proof_reopens_only_target_task(self):
  self.value.update(current_task=None,target_size=None,selected_ids=[],saved_ids=[],finished_tasks=['task','other'],finished_images={'task':['img','otherimg'],'other':['x']});self.value['image_refs']['otherimg']={'task':'task','relative_path':'other'};self.write();self.assertEqual(self.runrestore()['status'],'restored');v=json.loads(self.path.read_text());self.assertEqual(v['finished_tasks'],['other']);self.assertEqual(v['finished_images'],{'other':['x']})
 def test_old_saved_before_current_block_adjusts_start(self):
  self.value.update(selected_ids=[],saved_ids=[],saved_history=['img'],block_saved_start=1);self.write();self.assertEqual(self.runrestore()['status'],'restored');self.assertEqual(json.loads(self.path.read_text())['block_saved_start'],0)
 def test_missing_evidence_is_manual(self):
  self.assertEqual(self.runrestore(Ledger([]))['status'],'manual');self.assertEqual(json.loads(self.path.read_text()),self.value)
 def test_active_pending_target_is_manual(self):
  self.value.update(pending='img',saved_ids=[]);self.write();self.assertEqual(self.runrestore()['status'],'manual');self.assertEqual(json.loads(self.path.read_text()),self.value)
 def test_malformed_unhashable_state_is_manual(self):
  self.value['saved_ids']=[{'bad':'value'}];self.write();self.assertEqual(self.runrestore()['status'],'manual');self.assertEqual(json.loads(self.path.read_text()),self.value)
 def test_partial_write_replays_all_scopes(self):
  import task_delete_restore as mod
  scoped=self.base/'runtime/text_task_blocks/blocks-temporary-test.json';scoped.parent.mkdir();scoped.write_text(json.dumps(self.value))
  durable=mod._durable;calls=[]
  def crash_second(path,data):
   calls.append(path)
   if len(calls)==2:raise OSError('simulated power loss')
   durable(path,data)
  with patch.object(mod,'_durable',side_effect=crash_second):
   with self.assertRaises(OSError):self.runrestore()
  self.assertEqual(self.runrestore()['status'],'restored')
  for path in (self.path,scoped):self.assertEqual(json.loads(path.read_text())['saved_history'],[])
  self.assertEqual(self.runrestore()['status'],'restored')
 def test_no_matching_task_is_manual(self):
  self.value['image_refs']={};self.write();self.assertEqual(self.runrestore()['status'],'manual')
unittest.main()
