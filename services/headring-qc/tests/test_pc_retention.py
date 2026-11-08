import hashlib,json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'services/headring-qc'))
from pc_retention import Retention,DAYS

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        self.data=root/'data';self.source=self.data/'standardized/cp_0020/2026-11-08/L2-020-fixture';self.source.mkdir(parents=True)
        self.files=[]
        for name,data in [('audios/.keep',b''),('meta/info.json',b'{}')]:
            p=self.source/name;p.parent.mkdir(exist_ok=True);p.write_bytes(data)
            self.files.append(dict(path=name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
        self.retention=Retention(root/'state');self.stamp=1000
        receipt=dict(dataset='EGOL2/cp_0020/2026-11-08/L2-020-fixture',cloud_state='remote_confirmed',cloud_receipt_verified=True,
            cloud_confirmed_at=self.stamp,files=len(self.files),objects=[{'key':'data/EGOL2/cp_0020/2026-11-08/L2-020-fixture/'+r['path'],'bytes':r['bytes']} for r in self.files])
        self.receipt=self.retention.seal(receipt,self.source,self.files,'TEST-VOLUME')
    def test_full_72_hours_required_not_pc_receive_time(self):
        result=self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS-1)
        self.assertEqual(result['state'],'held');self.assertTrue(self.source.exists())
    def test_expired_exact_package_removed_other_data_retained(self):
        other=self.data/'standardized/other';other.mkdir();(other/'keep').write_bytes(b'untouched')
        result=self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS)
        self.assertEqual(result['state'],'deleted');self.assertFalse(self.source.exists())
        self.assertEqual((other/'keep').read_bytes(),b'untouched')
        self.assertEqual(result,self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS+1))
    def test_failed_or_unconfirmed_upload_cannot_start_retention(self):
        bad=dict(self.receipt,cloud_state='FAILED');bad.pop('signature')
        with self.assertRaises(ValueError):self.retention.seal(bad,self.source,self.files,'TEST-VOLUME')
        self.assertTrue(self.source.exists())
    def test_modified_receipt_or_wrong_drive_retains(self):
        with self.assertRaises(ValueError):self.retention.cleanup(dict(self.receipt,pc_delete_after=0),self.data,'TEST-VOLUME',now=999999)
        with self.assertRaises(ValueError):self.retention.cleanup(self.receipt,self.data,'OTHER-VOLUME',now=999999)
        self.assertTrue(self.source.exists())
    def test_modified_files_and_added_files_retained(self):
        (self.source/'user-edit').write_bytes(b'do not discard')
        with self.assertRaises(ValueError):self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=999999)
        self.assertTrue(self.source.exists())
    def test_partial_cleanup_recovered_from_committed_journal(self):
        def interrupted(target):
            next(p for p in Path(target).rglob('*') if p.is_file()).unlink();raise OSError('simulated interruption')
        with patch('pc_retention.shutil.rmtree',side_effect=interrupted):
            with self.assertRaises(OSError):self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS)
        result=self.retention.cleanup(self.receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS+1)
        self.assertEqual(result['state'],'deleted')
    def test_related_raw_copy_needs_lineage_and_same_cloud_deadline(self):
        raw=self.data/'originals/cp_0020/received/2026-11-08/L2-020-fixture';raw.mkdir(parents=True)
        (raw/'raw.bin').write_bytes(b'original')
        files=[dict(path='raw.bin',bytes=8,sha256=hashlib.sha256(b'original').hexdigest())]
        with self.assertRaises(ValueError):self.retention.related(self.receipt,raw,files,'originals',{})
        receipt=self.retention.related(self.receipt,raw,files,'originals',
            {'raw_handoff_receipt_sha256':'a'*64,'archive_manifest_digest':'b'*64})
        self.assertEqual(self.retention.cleanup(receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS-1)['state'],'held')
        self.assertTrue(raw.exists())
        self.assertEqual(self.retention.cleanup(receipt,self.data,'TEST-VOLUME',now=self.stamp+DAYS)['state'],'deleted')

if __name__=='__main__':unittest.main(verbosity=2)
