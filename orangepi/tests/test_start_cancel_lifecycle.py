"""Exercise real dashboard queue and privacy gate without hardware or device I/O."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
for path in ('common','control','acquisition','storage','audio'):
    sys.path.insert(0,str(ROOT/path))
import dashboard


class StartCancelLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();base=Path(self.tmp.name);(base/'runtime').mkdir()
        self.ui=dashboard.Dashboard(base,base,{'host':'127.0.0.1','port':0})
        self.ui.camera_checks.approved=lambda:True
        self.ui.publish({'state':'ready','revision':7})
        self.rid='a'*32
    def tearDown(self):
        self.ui.close();self.tmp.cleanup()
    def start(self):
        self.ui.submit(dict(action='start',revision=7,request_id=self.rid,stop_protocol='session-stop-v1',privacy_instance=self.ui.privacy.instance))
    def cancel(self,request_id=None,instance=None):
        self.ui.submit(dict(action='cancel_start',request_id=request_id or self.rid,
            privacy_instance=instance or self.ui.privacy.instance))
    def arm(self):
        p=self.ui.privacy
        p.display({'client_id':'screen','stop_protocol':'session-stop-v1'})
        return self.ui.arm_start('/session',worker_pid=os.getpid(),adapter_pid=os.getpid(),owner_run='owner')
    def commit(self):
        with patch('screen_privacy.boot_identity',return_value='boot'), patch.object(self.ui.privacy,'ready',return_value=True):
            return self.ui.commit_start()
    def test_cancel_before_request_tombstones_late_start(self):
        self.cancel()
        with self.assertRaises(ValueError):self.start()
        self.assertIsNone(self.ui.take())
        self.assertFalse(self.ui.display_state()['terminal_results'][-1]['ok'])
    def test_old_controller_start_cannot_arrive_after_restart(self):
        with self.assertRaisesRegex(ValueError,'控制器已变化'):
            self.ui.submit(dict(action='start',revision=7,request_id=self.rid,stop_protocol='session-stop-v1',privacy_instance='old'))
        self.assertIsNone(self.ui.take())
    def test_cancel_queued_start_removes_it_without_worker_execution(self):
        self.start();self.cancel()
        self.assertIsNone(self.ui.take())
        result=self.ui.display_state()['terminal_results'][-1]
        self.assertEqual((result['request_id'],result['commit_state']),(self.rid,'not_committed'))
    def test_cancel_executing_start_before_arm_and_during_arm(self):
        self.start();self.ui.take();self.cancel()
        self.assertFalse(self.arm())
        self.ui.complete('cancelled')
        self.rid='b'*32;self.start();self.ui.take();self.assertTrue(self.arm());self.cancel()
        self.assertTrue(self.ui.privacy.cancelled)
        with self.assertRaises(ValueError):self.commit()
        self.assertFalse(self.ui.privacy.committed)
    def test_commit_before_cancel_prioritizes_bound_stop_and_preserves_pending(self):
        self.start();self.ui.take();self.assertTrue(self.arm());self.commit();self.ui.complete()
        self.ui.submit(dict(action='repeat',revision=7))
        self.cancel()
        self.ui.submit(dict(action='stop',privacy_target=self.ui.start_targets[self.rid]))
        job=self.ui.take()
        self.assertEqual(job['action'],'stop')
        self.assertEqual(job['request_id'],self.rid)
        self.assertEqual(job['privacy_target']['session_id'],'/session')
        self.assertEqual(self.ui.pending['action'],'repeat')
    def test_cancel_commit_race_has_exactly_one_safe_winner(self):
        self.start();self.ui.take();self.assertTrue(self.arm())
        barrier=threading.Barrier(2);result=[]
        def commit():
            barrier.wait()
            try:self.commit();result.append('committed')
            except ValueError:result.append('cancelled')
        thread=threading.Thread(target=commit);thread.start();barrier.wait();self.cancel();thread.join(5)
        self.assertFalse(thread.is_alive())
        if result==['committed']:
            self.assertTrue(self.ui.privacy.committed);self.assertEqual(self.ui.pending_stop['action'],'stop')
        else:
            self.assertEqual(result,['cancelled']);self.assertFalse(self.ui.privacy.committed)
    def test_full_tombstones_fail_closed_and_allow_existing_start_cancellation(self):
        self.start();self.ui.cancelled_starts={format(i,'032x') for i in range(256)}
        self.cancel();self.assertIsNone(self.ui.take())
        self.rid='b'*32
        with self.assertRaisesRegex(ValueError,'记录已满'):self.start()
        terminal=self.ui.display_state()['terminal_results'][-1]
        self.assertEqual((terminal['request_id'],terminal['commit_state']),('b'*32,'not_committed'))
    def test_old_request_cannot_stop_new_session_and_same_stopped_target_is_idempotent(self):
        self.start();self.ui.take();self.assertTrue(self.arm());self.commit();self.ui.complete()
        p=self.ui.privacy;target=dict(self.ui.start_targets[self.rid])
        p.last_stopped_target=dict(target);p.phase='idle';p.nonce='';p.committed=False
        self.cancel();self.assertIsNone(self.ui.pending_stop)
        self.ui.submit(dict(action='stop',privacy_target=target));self.assertIsNone(self.ui.pending_stop)
        p.phase='recording';p.nonce='newnonce';p.session='/new';p.committed=True;p.generation+=3
        with self.assertRaises(ValueError):self.cancel()
        self.assertIsNone(self.ui.pending_stop)
        with self.assertRaises(ValueError):self.cancel(instance='old-controller')


if __name__=='__main__':unittest.main()
