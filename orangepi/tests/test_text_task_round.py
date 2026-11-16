import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control')]
from task_block_sampling import TaskBlocks

class TextRoundTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);(self.base/'runtime').mkdir()
  self.ids=['list_%032x'%i for i in range(20)];self.settings={'list_ids':self.ids,'block_size':5,'round_robin':True,'namespace':'temporary-fixture'}
  self.items=[{'id':'text_%032x'%(t*50+n),'relative_path':'task%d/occ%d'%(t,n),'source_lists':[{'id':self.ids[t],'name':'task%d'%t}]} for t in range(20) for n in range(50)]
  self.snapshot={'items':self.items};self.done=set();self.choice=patch('task_block_sampling.secrets.choice',side_effect=lambda values:values[0]);self.choice.start();self.addCleanup(self.choice.stop)
 def entries(self):return [dict(snapshot=self.snapshot,index=i) for i,x in enumerate(self.items) if x['relative_path'] not in self.done]
 def complete(self,blocks):
  blocks.reconcile(self.snapshot,self.done);entry=blocks.choose(self.entries(),lambda rows:rows[0]);item=self.items[entry['index']];self.done.add(item['relative_path']);blocks.success(entry);return blocks.task(entry),item
 def test_twenty_tasks_ten_rounds_and_stop(self):
  b=TaskBlocks(self.base,self.settings);tasks=[]
  for i in range(1000):
   task,_=self.complete(b);tasks.append(task)
   if i in (3,4,499):b=TaskBlocks(self.base,self.settings)
  for round_number in range(10):
   for task in range(20):self.assertEqual(tasks[round_number*100+task*5:round_number*100+task*5+5],[self.ids[task]]*5)
  b.reconcile(self.snapshot,self.done)
  with self.assertRaises(ValueError):b.choose(self.entries(),lambda rows:rows[0])
 def test_failure_pending_and_restart_keep_occurrence(self):
  b=TaskBlocks(self.base,self.settings)
  for _ in range(4):self.complete(b)
  b.reconcile(self.snapshot,self.done);entry=b.choose(self.entries(),lambda rows:rows[0]);first=self.items[entry['index']]['id']
  b=TaskBlocks(self.base,self.settings);b.reconcile(self.snapshot,self.done)
  retry=b.choose(self.entries(),lambda rows:rows[-1]);self.assertEqual(self.items[retry['index']]['id'],first)
  available=[x for x in self.entries() if self.items[x['index']]['id']!=first]
  with self.assertRaises(ValueError):b.choose(available,lambda rows:rows[0])
  item=self.items[retry['index']];self.done.add(item['relative_path']);b.success(retry)
  task,_=self.complete(b);self.assertEqual(task,self.ids[1])
 def test_pending_qc_after_restart_counts_only_after_qualified(self):
  b=TaskBlocks(self.base,self.settings)
  for _ in range(4):self.complete(b)
  b.reconcile(self.snapshot,self.done);entry=b.choose(self.entries(),lambda rows:rows[0]);item=self.items[entry['index']]
  b=TaskBlocks(self.base,self.settings);b.reconcile(self.snapshot,self.done)
  self.assertEqual(len(b.value['completed_ids']),4);self.assertEqual(b.value['current_task'],self.ids[0])
  self.done.add(item['relative_path']);b.reconcile(self.snapshot,self.done)
  self.assertIsNone(b.value['current_task']);task,_=self.complete(b);self.assertEqual(task,self.ids[1])
 def test_late_qc_reversal_and_last_one_complete_block(self):
  for t in range(20):
   for n in range(5):self.done.add('task%d/occ%d'%(t,n))
  self.done.remove('task0/occ4');b=TaskBlocks(self.base,self.settings);b.reconcile(self.snapshot,self.done)
  entry=b.choose(self.entries(),lambda rows:rows[0]);self.assertEqual(b.task(entry),self.ids[0]);self.assertEqual(b.value['target_size'],1)
  self.done.add(self.items[entry['index']]['relative_path']);b.success(entry)
  for t in range(20):
   for n in range(49):self.done.add('task%d/occ%d'%(t,n))
  b=TaskBlocks(self.base,self.settings);b.reconcile(self.snapshot,self.done);entry=b.choose(self.entries(),lambda rows:rows[0]);self.assertEqual(b.value['target_size'],1)
if __name__=='__main__':unittest.main()
