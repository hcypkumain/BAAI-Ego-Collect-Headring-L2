import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control')]
from task_block_sampling import TaskBlocks,ensure_default_policy


class GeneralSavedTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
  (self.base/'runtime').mkdir();(self.base/'configs').mkdir()
  self.ids=['list_a','list_b'];self.settings=dict(list_ids=self.ids,block_size=5)
  self.items=[dict(id='image_%s_%d'%(task,i),relative_path='%s/%d.png'%(task,i),source_lists=[dict(id=task)]) for task in self.ids for i in range(50)]
  self.snapshot=dict(items=self.items);self.entries=[dict(snapshot=self.snapshot,index=i) for i in range(len(self.items))]
  self.pick=patch('task_block_sampling.secrets.choice',side_effect=lambda values:values[0]);self.pick.start();self.addCleanup(self.pick.stop)
 def saved(self,blocks):
  blocks.reconcile(self.snapshot,[],[])
  entry=blocks.choose(self.entries,lambda rows:rows[0]);task=blocks.task(entry);blocks.defer(entry);return task
 def test_existing_n_without_basis_defaults_to_saved(self):
  blocks=TaskBlocks(self.base,dict(self.settings,block_size=3))
  self.assertEqual(blocks.count_basis,'saved')
  self.assertEqual([self.saved(blocks) for _ in range(4)],['list_a']*3+['list_b'])
  self.assertEqual(blocks.value['completed_ids'],[])
 def test_text_saves_rotate_without_receipts_and_survive_restart(self):
  settings=dict(self.settings,round_robin=True,namespace='temporary-general')
  blocks=TaskBlocks(self.base,settings);tasks=[]
  for i in range(100):
   if i in (3,5,51):blocks=TaskBlocks(self.base,settings)
   tasks.append(self.saved(blocks))
  self.assertEqual(tasks,[task for _ in range(10) for task in self.ids for _ in range(5)])
  self.assertEqual(blocks.round_counts(),{'list_a':50,'list_b':50})
  self.assertEqual(blocks.value['completed_ids'],[])
  blocks.reconcile(self.snapshot,[],[])
  with self.assertRaises(ValueError):blocks.choose(self.entries,lambda rows:rows[0])
 def test_text_namespaces_and_image_progress_are_independent(self):
  image=TaskBlocks(self.base,self.settings);self.saved(image);before=image.path.read_bytes()
  a=TaskBlocks(self.base,dict(self.settings,round_robin=True,namespace='temporary-a'));self.saved(a)
  b=TaskBlocks(self.base,dict(self.settings,round_robin=True,namespace='temporary-b'))
  self.assertNotEqual(a.path,b.path);self.assertEqual(b.round_counts(),{'list_a':0,'list_b':0})
  self.assertEqual(image.path.read_bytes(),before)
 def test_scope_retirement_does_not_erase_text_save_history(self):
  settings=dict(self.settings,round_robin=True,namespace='temporary-keep')
  b=TaskBlocks(self.base,settings)
  for _ in range(5):self.saved(b)
  b=TaskBlocks(self.base,settings);b.reconcile({'items':[]},[],[])
  self.assertEqual(b.round_counts()['list_a'],5)
 def catalog(self):
  return dict(list_order=['list_a','list_b','hidden','off','text'],lists={
   'list_a':dict(items=['a']), 'list_b':dict(items=['b']),
   'hidden':dict(items=['a'],hidden=True),'off':dict(items=['b'],sampling_enabled=False),
   'text':dict(items=['t'],text_task_namespace='temporary-x')},
   images={'a':dict(kind='image'),'b':dict(kind='image'),'t':dict(kind='text')})
 def test_bootstrap_uses_local_enabled_image_lists(self):
  p=ensure_default_policy(self.base,self.catalog())
  self.assertEqual(p,dict(version=1,list_ids=self.ids,block_size=5,count_basis='saved'))
 def test_bootstrap_preserves_existing_n_and_list_scope(self):
  path=self.base/'configs/task_block_policy.json';original=dict(version=1,list_ids=['list_b'],block_size=7)
  path.write_text(json.dumps(original));before=path.read_bytes()
  self.assertEqual(ensure_default_policy(self.base,self.catalog()),original)
  self.assertEqual(path.read_bytes(),before)
 def test_text_only_catalog_does_not_create_image_policy(self):
  catalog=self.catalog();catalog['lists']={'text':catalog['lists']['text']};catalog['list_order']=['text']
  self.assertIsNone(ensure_default_policy(self.base,catalog))
  self.assertFalse((self.base/'configs/task_block_policy.json').exists())
 def test_explicit_qualified_legacy_mode_remains_available(self):
  b=TaskBlocks(self.base,dict(self.settings,count_basis='qualified'))
  for _ in range(5):b.defer(b.choose(self.entries,lambda rows:rows[0]))
  with self.assertRaisesRegex(ValueError,'等待质检'):b.choose(self.entries,lambda rows:rows[0])
 def test_state_v3_upgrades_without_losing_saved_ids(self):
  b=TaskBlocks(self.base,self.settings);self.saved(b);state=json.loads(b.path.read_text())
  state['version']=3;state.pop('saved_history');b.path.write_text(json.dumps(state))
  restored=TaskBlocks(self.base,self.settings)
  self.assertEqual(restored.value['version'],4)
  self.assertEqual(restored.value['saved_ids'],state['saved_ids'])
  self.assertEqual(restored.value['saved_history'],state['saved_ids'])
 def legacy_text(self,count):
  settings=dict(self.settings,round_robin=True,namespace='temporary-legacy')
  path=self.base/'runtime/text_task_blocks/blocks-temporary-legacy.json';path.parent.mkdir()
  pending='image_list_a_%d'%(count-1)
  path.write_text(json.dumps(dict(version=1,current_task='list_a',target_size=5,completed_ids=[],finished_tasks=[],pending=pending)))
  blocks=TaskBlocks(self.base,settings)
  reserved=['list_a/%d.png'%i for i in range(count)]
  blocks.reconcile(self.snapshot,[],reserved)
  return blocks
 def test_legacy_text_five_saved_pending_without_receipts_finishes_block(self):
  blocks=self.legacy_text(5)
  self.assertIsNone(blocks.value['current_task'])
  self.assertEqual(blocks.round_counts()['list_a'],5)
  self.assertEqual(blocks.task(blocks.choose(self.entries,lambda rows:rows[0])),'list_b')
 def test_legacy_text_four_saved_keeps_only_one_remaining(self):
  blocks=self.legacy_text(4)
  self.assertEqual(len(blocks.value['saved_ids']),4)
  self.assertEqual(blocks.value['completed_ids'],[])
  entry=blocks.choose(self.entries,lambda rows:rows[0]);blocks.defer(entry)
  self.assertIsNone(blocks.value['current_task'])
  self.assertEqual(blocks.round_counts()['list_a'],5)

if __name__=='__main__':unittest.main()
