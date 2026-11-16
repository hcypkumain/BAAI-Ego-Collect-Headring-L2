import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/orangepi_collect/control'))
from usb_export_jobs import ExportJob, STAGES
from cloud_common import CloudError

class JobTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.calls=[]
  self.handlers={state:self.handler(state) for state in STAGES[:-1]}
  self.job=ExportJob(self.tmp.name,self.handlers)
 def handler(self,state):
  def go(value,progress): self.calls.append(state)
  return go
 def test_complete_persisted_and_replay_safe(self):
  self.job.create({'uuid':'one'});self.assertEqual(self.job.run()['state'],'complete')
  self.assertEqual(self.calls,list(STAGES[:-1]));self.job.run();self.assertEqual(len(self.calls),6)
 def test_power_loss_resumes_at_interrupted_stage(self):
  self.job.create({'uuid':'one'})
  def fail(value,progress): raise OSError('power loss')
  self.job.handlers['verifying']=fail
  with self.assertRaises(OSError):self.job.run()
  self.assertEqual(self.job.status()['state'],'verifying')
  recovered=ExportJob(self.tmp.name,self.handlers.copy()); recovered.handlers['verifying']=self.handler('verifying')
  self.assertEqual(recovered.run()['state'],'complete')
 def test_missing_handler_fails_closed(self):
  self.job.create({'uuid':'one'});self.job.handlers={}
  with self.assertRaises(CloudError):self.job.run()
  self.assertEqual(self.job.status()['state'],'preparing')
 def test_progress_is_durable(self):
  self.job.create({'uuid':'one'});self.job.progress(5,10)
  self.assertEqual(ExportJob(self.tmp.name,{}).status()['done_bytes'],5)
