import sys, unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src/orangepi_collect'/name) for name in ('control','common')]
from cloud_uploads import UploadManager

class CloudGateTests(unittest.TestCase):
 def test_pending_export_blocks_upload_before_display_startup(self):
  manager=UploadManager.__new__(UploadManager);manager.display_state=Mock(side_effect=AssertionError('must not inspect display'))
  with patch('usb_export_gate.pending_exports',return_value=[{'state':'copying'}]):
   self.assertTrue(manager.collection_busy())
  with patch('usb_export_gate.pending_exports',side_effect=OSError('unreadable')):
   self.assertTrue(manager.collection_busy())
 def test_cached_job_is_not_launched_after_pause_interleaves(self):
  manager=UploadManager.__new__(UploadManager)
  manager.active=None;manager.process=None;manager.state={'paused':False,'jobs':[{'state':'queued'}]}
  manager.stop=Mock();manager.stop.wait.side_effect=[False,True];manager.stop.is_set.return_value=False
  manager.collection_busy=Mock(return_value=False);manager._launch=Mock();manager._save=Mock()
  class Lock:
   count=0
   def __enter__(self):return self
   def __exit__(self,*args):
    self.count+=1
    if self.count==1:manager.state['paused']=True
  manager.lock=Lock();manager._run();manager._launch.assert_not_called()
