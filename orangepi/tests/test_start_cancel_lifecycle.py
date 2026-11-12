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
        self.ticket=self.issue(self.rid)
    def tearDown(self):
        self.ui.close();self.tmp.cleanup()
    def start(self):
        self.ui.submit(dict(action='start',revision=7,request_id=self.rid,stop_protocol='session-stop-v1',privacy_instance=self.ui.privacy.instance,**self.ticket))
    def issue(self,rid):
        return self.ui.submit(dict(action='start_ticket',request_id=rid,start_ticket=self.ui.ticket_next,privacy_instance=self.ui.privacy.instance))
    def cancel(self,request_id=None,instance=None):
        self.ui.submit(dict(action='cancel_start',request_id=request_id or self.rid,
            privacy_instance=instance or self.ui.privacy.instance,**self.ticket))
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
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);(base/'runtime').mkdir()
            replacement=dashboard.Dashboard(base,base,{'host':'127.0.0.1','port':0})
            try:
                replacement.publish({'state':'ready','revision':7});replacement.camera_checks.approved=lambda:True
                with self.assertRaises(ValueError):
                    replacement.submit(dict(action='start',revision=7,request_id=self.rid,stop_protocol='session-stop-v1',privacy_instance=self.ui.privacy.instance,**self.ticket))
                self.assertIsNone(replacement.take())
            finally:replacement.close()
    def test_cancel_queued_start_removes_it_without_worker_execution(self):
        self.start();self.cancel()
        self.assertIsNone(self.ui.take())
        result=self.ui.display_state()['terminal_results'][-1]
        self.assertEqual((result['request_id'],result['commit_state']),(self.rid,'not_committed'))
    def test_cancel_executing_start_before_arm_and_during_arm(self):
        self.start();self.ui.take();self.cancel()
        self.assertFalse(self.arm())
        self.ui.complete('cancelled')
        self.rid='b'*32;self.ticket=self.issue(self.rid);self.start();self.ui.take();self.assertTrue(self.arm());self.cancel()
        self.assertTrue(self.ui.privacy.cancelled)
        with self.assertRaises(ValueError):self.commit()
        self.assertFalse(self.ui.privacy.committed)
    def test_commit_before_cancel_prioritizes_bound_stop_and_preserves_pending(self):
        self.start();self.ui.take();self.assertTrue(self.arm());self.commit();self.ui.complete()
        self.ui.submit(dict(action='repeat',revision=7))
        self.cancel()
        self.ui.submit(dict(action='stop',privacy_target=self.ui.start_targets[(self.ticket['start_ticket'],self.rid)]))
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
    def test_over_1000_cancelled_tickets_cannot_revive_and_next_start_works(self):
        first=dict(self.ticket);first_id=self.rid
        self.cancel()
        for i in range(1100):
            self.rid=format(i,'032x');self.ticket=self.issue(self.rid);self.cancel()
        self.assertEqual(self.ui.ticket_watermark,1101)
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='start_ticket',request_id=first_id,start_ticket=first['start_ticket'],privacy_instance=self.ui.privacy.instance))
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='start',revision=7,request_id=first_id,stop_protocol='session-stop-v1',privacy_instance=self.ui.privacy.instance,**first))
        self.rid='b'*32;self.ticket=self.issue(self.rid);self.start()
        self.assertEqual(self.ui.take()['request_id'],self.rid)
    def test_out_of_order_cancel_does_not_invalidate_earlier_ticket(self):
        later_id='b'*32;later=self.issue(later_id)
        self.ui.submit(dict(action='cancel_start',request_id=later_id,privacy_instance=self.ui.privacy.instance,**later))
        self.assertEqual(self.ui.ticket_watermark,0)
        self.start();self.assertEqual(self.ui.take()['request_id'],self.rid)
        self.ui.complete('rejected')
        self.assertEqual(self.ui.ticket_watermark,2)
    def test_issued_window_full_is_explicit_and_cancellation_releases_space(self):
        for i in range(1,64):self.issue(format(i,'032x'))
        with self.assertRaisesRegex(ValueError,'票据已满'):self.issue('f'*32)
        self.cancel()
        self.assertEqual(self.ui.ticket_watermark,1)
        self.assertEqual(self.issue('f'*32)['start_ticket'],65)
    def test_unissued_wrong_proof_and_repeated_posts(self):
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='cancel_start',request_id=self.rid,privacy_instance=self.ui.privacy.instance,start_ticket=999,ticket_proof='0'*64))
        repeated=self.ui.submit(dict(action='start_ticket',request_id=self.rid,start_ticket=self.ticket['start_ticket'],privacy_instance=self.ui.privacy.instance))
        self.assertEqual(repeated,self.ticket)
        self.start();self.start();self.assertEqual(self.ui.take()['request_id'],self.rid)
        self.cancel();self.cancel();self.ui.complete('cancelled');self.cancel()
        with self.assertRaises(ValueError):self.start()
    def test_wrong_instance_or_context_cannot_issue_or_cancel(self):
        self.ui.publish({'state':'ready','revision':7,'collection_context':'eeg:1'})
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='start_ticket',request_id='b'*32,start_ticket=2,privacy_instance='old',collection_context='eeg:1'))
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='start_ticket',request_id='b'*32,start_ticket=2,privacy_instance=self.ui.privacy.instance,collection_context='emg:2'))
        with self.assertRaises(ValueError):
            self.ui.submit(dict(action='cancel_start',request_id=self.rid,privacy_instance='old',collection_context='eeg:1',**self.ticket))
        self.assertEqual(self.ui.ticket_watermark,0)
    def test_old_ticket_with_reused_request_id_cannot_stop_new_session(self):
        old=dict(self.ticket);self.cancel()
        for i in range(70):
            rid=format(i,'032x');ticket=self.issue(rid)
            self.ui.submit(dict(action='cancel_start',request_id=rid,privacy_instance=self.ui.privacy.instance,**ticket))
        self.ticket=self.issue(self.rid);self.start();self.ui.take();self.assertTrue(self.arm());self.commit();self.ui.complete()
        self.ui.submit(dict(action='cancel_start',request_id=self.rid,privacy_instance=self.ui.privacy.instance,**old))
        self.assertIsNone(self.ui.pending_stop)
        self.assertEqual(self.ui.privacy.phase,'arming')
    def test_old_request_cannot_stop_new_session_and_same_stopped_target_is_idempotent(self):
        self.start();self.ui.take();self.assertTrue(self.arm());self.commit();self.ui.complete()
        p=self.ui.privacy;target=dict(self.ui.start_targets[(self.ticket['start_ticket'],self.rid)])
        p.last_stopped_target=dict(target);p.phase='idle';p.nonce='';p.committed=False
        self.cancel();self.assertIsNone(self.ui.pending_stop)
        self.ui.submit(dict(action='stop',privacy_target=target));self.assertIsNone(self.ui.pending_stop)
        p.phase='recording';p.nonce='newnonce';p.session='/new';p.committed=True;p.generation+=3
        with self.assertRaises(ValueError):self.cancel()
        self.assertIsNone(self.ui.pending_stop)
        with self.assertRaises(ValueError):self.cancel(instance='old-controller')


if __name__=='__main__':unittest.main()
