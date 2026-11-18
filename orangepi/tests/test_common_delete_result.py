import threading,unittest
from dashboard import Dashboard

class DeleteResultTests(unittest.TestCase):
 def make(self,action):
  dash=Dashboard.__new__(Dashboard);dash.lock=threading.Lock();dash.active_action=action;dash.active_task_block_job=None;dash.busy=True;return dash
 def test_successful_deletion_reports_manual_restoration_separately(self):
  dash=self.make('delete_recording');details={'deleted':True,'message':'已删除本段采集，但参考图进度未自动恢复：历史证据不足','reference_restoration':{'status':'manual','reason':'历史证据不足'}}
  dash.complete(action_details=details)
  self.assertTrue(dash.result['ok']);self.assertEqual(dash.result['reference_restoration']['status'],'manual');self.assertIn('已删除',dash.result['message'])
 def test_other_action_cannot_leak_delete_details(self):
  dash=self.make('prepare');dash.complete(action_details={'deleted':True,'message':'不应显示'})
  self.assertNotIn('message',dash.result)

if __name__=='__main__':unittest.main()
