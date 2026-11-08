import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'services/headring-qc'))
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/common'))
from receiver_state import ReceiverStore,Conflict
from raw_handoff import (signed,sha_value,validate_raw_receipt,pc_cleanup_eligible,
                         pc_retention_deadline,RAW_PURPOSE)
import cloud_scope

KEY=b'synthetic-unit-test-key-not-a-credential'

def manifest():
    rows=[dict(path=name,bytes=len(data),mtime_ns=123,inode=index,
               sha256=hashlib.sha256(data).hexdigest())
          for index,(name,data) in enumerate([('frames.bin',b'video'),('native_capture_summary.json',b'{}')])]
    return dict(files=rows,total_files=2,total_bytes=7,digest=sha_value(rows),
        native_sha256=rows[1]['sha256'],device_id='cp_0020',session_id='L2-020-test',root='/source')

def receipt():
    return dict(version=1,purpose=RAW_PURPOSE,device_id='cp_0020',server_id='server-test',
        session_id='L2-020-test',manifest=manifest(),host_verified=True,host_fsynced=True,
        host_path='/host/received/L2-020-test',host_storage_uuid='SYNTHETIC',transaction_id=uuid.uuid4().hex)

class ProtocolTests(unittest.TestCase):
    def verify(self,value):return validate_raw_receipt(value,KEY,'cp_0020','server-test','/source')
    def test_complete_byte_receipt_does_not_require_qc_pass(self):
        value=receipt();value['qc_level']='ERROR';self.verify(signed(value,KEY))
    def test_wrong_purpose_or_signature_is_refused(self):
        value=signed(receipt(),KEY);value['purpose']='qc_archive'
        with self.assertRaises(ValueError):self.verify(value)
        value=signed(receipt(),KEY);value['manifest']['total_bytes']=999
        with self.assertRaises(ValueError):self.verify(value)
    def test_bad_sizes_paths_counts_digest_and_identity_refused(self):
        for kind in ['size','count','digest','escape','duplicate','native','device','server','fsync']:
            with self.subTest(kind=kind):
                value=receipt();m=value['manifest']
                if kind=='size':m['files'][0]['bytes']=-1
                elif kind=='count':m['total_files']=3
                elif kind=='digest':m['digest']='0'*64
                elif kind=='escape':m['files'][0]['path']='../outside'
                elif kind=='duplicate':m['files'][1]['path']=m['files'][0]['path']
                elif kind=='native':m['native_sha256']='0'*64
                elif kind=='device':value['device_id']='cp_0013'
                elif kind=='server':value['server_id']='another-server'
                else:value['host_fsynced']=False
                with self.assertRaises(ValueError):self.verify(signed(value,KEY))
    def test_retention_starts_only_at_verified_cloud_success(self):
        now=1000000
        for state in ['not_configured','queued','uploading','FAILED']:
            self.assertFalse(pc_cleanup_eligible(dict(cloud_state=state,cloud_confirmed_at=1,
                cloud_receipt_verified=True,retention_hold=False),now,True))
        row=dict(cloud_state='remote_confirmed',cloud_confirmed_at=now,
                 cloud_receipt_verified=True,retention_hold=False)
        self.assertFalse(pc_cleanup_eligible(row,now+259199,True))
        self.assertTrue(pc_cleanup_eligible(row,now+259200,True))
        self.assertFalse(pc_cleanup_eligible(row,now+999999,False))
        self.assertFalse(pc_cleanup_eligible(dict(row,cloud_receipt_verified=False),now+999999,True))
        self.assertFalse(pc_cleanup_eligible(dict(row,retention_hold=True),now+999999,True))
    def test_invalid_cloud_confirmation_never_uses_received_or_mtime(self):
        for stamp in [None,False,-1,0,float('nan'),'2026-11-08']:
            with self.assertRaises(ValueError):pc_retention_deadline(stamp)
            row=dict(cloud_state='remote_confirmed',cloud_confirmed_at=stamp,
                received_at=1,mtime=1,cloud_receipt_verified=True,retention_hold=False)
            self.assertFalse(pc_cleanup_eligible(row,9999999,True))

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ReceiverStore(Path(self.temp.name)/'inner-ledger',clock=lambda:1000)
    def operation(self):return uuid.uuid4().hex
    def test_manual_default_stop_and_persistent_start(self):
        self.assertFalse(self.store.control()['receiving'])
        self.store.set_control(True,0,self.operation())
        other=ReceiverStore(self.store.root,clock=lambda:1000)
        self.assertTrue(other.control()['receiving'])
    def test_duplicate_start_after_stop_returns_current_stop_not_old_start(self):
        operation=self.operation();self.store.set_control(True,0,operation)
        self.store.set_control(False,1,self.operation())
        value=self.store.set_control(True,0,operation)
        self.assertFalse(value['receiving']);self.assertTrue(value['idempotent'])
        self.assertEqual(value['operation_revision'],1)
    def test_stale_browser_cannot_undo_stop(self):
        self.store.set_control(True,0,self.operation())
        self.store.set_control(False,1,self.operation())
        with self.assertRaises(Conflict):self.store.set_control(True,1,self.operation())
    def test_auth_sessions_survive_restart_without_plaintext_token_in_db(self):
        token,csrf=self.store.login(self.store.admin_key,'test-client')
        self.assertTrue(self.store.authorized(token));self.assertEqual(csrf,self.store.csrf(token))
        self.assertTrue(ReceiverStore(self.store.root,clock=lambda:1000).authorized(token))
        with self.store.db() as c:self.assertNotEqual(c.execute('SELECT digest FROM auth_sessions').fetchone()[0],token)
    def test_login_throttling(self):
        for _ in range(8):
            with self.assertRaises(PermissionError):self.store.login('wrong','client')
        with self.assertRaises(PermissionError):self.store.login(self.store.admin_key,'client')
    def job(self):
        return self.store.enqueue('cp_0020',dict(session_id='L2-020-test',native_sha256='a'*64))
    def test_job_dedup_and_native_identity_conflict(self):
        first=self.job();self.assertEqual(first,self.job())
        with self.assertRaises(Conflict):self.store.enqueue('cp_0020',dict(session_id='L2-020-test',native_sha256='b'*64))
        with self.assertRaises(ValueError):self.store.enqueue('cp_0007',dict(session_id='L2-007-test'))
    def test_stop_blocks_receive_but_not_processing_a_committed_copy(self):
        identifier=self.job();self.assertIsNone(self.store.claim('receive'))
        self.store.update(identifier,raw_receipt={'test':True},process_state='queued')
        self.assertIsNotNone(self.store.claim('process'))
    def test_restart_recovers_running_jobs_not_deletion_proofs(self):
        identifier=self.job();self.store.set_control(True,0,self.operation())
        self.store.claim('receive');other=ReceiverStore(self.store.root)
        self.assertEqual(other.job(identifier)['receive_state'],'retry_wait')
        self.assertFalse(other.job(identifier).get('raw_receipt'))
    def test_bad_job_is_visible_and_cannot_be_claimed(self):
        identifier=self.job()
        with self.store.db() as c:c.execute('UPDATE jobs SET record=? WHERE id=?',('{broken',identifier))
        self.assertEqual(self.store.jobs()[0]['receive_state'],'blocked')
        self.store.set_control(True,0,self.operation());self.assertIsNone(self.store.claim('receive'))

class ScopeTests(unittest.TestCase):
    def test_only_two_exact_roots(self):
        self.assertEqual(cloud_scope.object_key('EGOL2/session','meta/info.json'),'data/EGOL2/session/meta/info.json')
        self.assertEqual(cloud_scope.destination_for_profile('L3','sample'),'EGOL3_hand/sample')
        for value in ['EGOL2_other/session','OTHER/session','EGOL2/../OTHER','EGOL2//session','EGOL2/%2e%2e/OTHER']:
            with self.assertRaises(ValueError):cloud_scope.dataset_path(value)
    def test_delete_only_egol2_snapshot(self):
        plan=cloud_scope.deletion_plan([dict(key='data/EGOL2/old/video.mp4',bytes=5)])
        self.assertEqual(len(plan),1)
        with self.assertRaises(ValueError):cloud_scope.scoped_key('data/EGOL3_hand/old/video.mp4',delete=True)
        with self.assertRaises(ValueError):cloud_scope.deletion_plan([dict(key='data/OTHER/video',bytes=5)])
    def test_out_of_prefix_server_result_aborts(self):
        with self.assertRaises(ValueError):cloud_scope.validate_listing([dict(key='data/EGOL2_extra/file',bytes=5)],'data/EGOL2/')

if __name__=='__main__':unittest.main(verbosity=2)
