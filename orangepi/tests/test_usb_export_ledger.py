import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/orangepi_collect/control'))
from cloud_common import CloudError, atomic, source_manifest
from usb_export_ledger import commit_credential, validate_credential

class LedgerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.base=self.root/'base';self.source=self.root/'sd'/'take';self.source.mkdir(parents=True)
  (self.source/'data').write_bytes(b'data');self.manifest=source_manifest(self.source)
  self.tx='a'*32;self.receipt=self.base/'runtime/usb_exports/receipts'/(self.tx+'.json')
  atomic(self.receipt,dict(state='verified',source=str(self.source),usb_uuid='USB',destination='take',manifest=self.manifest,transaction_id=self.tx,manifest_file='.headring-manifest-'+self.tx+'.json'))
  self.meta=dict(id='take',token='token',status='success',qualified=True,retention_kind='successful',reference_path='image',qc_state='completed',reference_state='collected')
  self.project=self.root/'project'; image=self.project/'configs/environments/image';image.parent.mkdir(parents=True);image.write_bytes(b'image')
  self.meta['reference_path']='configs/environments/image'
  snapshot=dict(id='take',status='success',qualified=True,retained=True,accepted_at_capture=True,has_data=True,start=1,duration=1)
  daily=patch('daily_collection.take_snapshot',return_value=snapshot);daily.start();self.addCleanup(daily.stop)
  self.store=SimpleNamespace(project=self.project,roots=lambda:(self.source.parent,self.root/'sd'),metadata=lambda source:self.meta)
  self.usb=SimpleNamespace(uuid='USB',check=lambda:None)
  for name in ('_verify','verify_manifest'):
   p=patch('usb_export_ledger.'+name);p.start();self.addCleanup(p.stop)
 def commit(self):return commit_credential(self.base,self.store,self.source,self.receipt,self.usb)
 def test_commit_replay_and_corruption(self):
  path=self.commit();self.assertEqual(self.commit(),path)
  self.assertTrue(validate_credential(self.base,self.source,self.manifest,'USB',destination='take',for_cleanup=True)['qualified'])
  value=json.loads(path.read_text());value['payload']['status']='failed';path.write_text(json.dumps(value))
  with self.assertRaises(CloudError):validate_credential(self.base,self.source,self.manifest,'USB',destination='take')
 def test_pending_retains_status_and_refuses_cleanup(self):
  self.meta.update(status='pending_qc',qualified=False,retention_kind='pending_qc',qc_state='pending',reference_state='pending_qc')
  self.commit();v=validate_credential(self.base,self.source,self.manifest,'USB',destination='take')
  self.assertFalse(v['qualified']);self.assertEqual(v['status'],'pending_qc')
  with self.assertRaises(CloudError):validate_credential(self.base,self.source,self.manifest,'USB',destination='take',for_cleanup=True)
 def test_failed_never_becomes_qualified(self):
  self.meta.update(status='error',qualified=False,retention_kind='unsuccessful',qc_state='completed',reference_state='uncollected')
  self.commit();self.assertFalse(validate_credential(self.base,self.source,self.manifest,'USB',destination='take')['qualified'])
 def test_restart_after_commit_before_acknowledgement(self):
  def commit_then_interrupt(path,value):
   atomic(path,value);raise OSError('power loss after commit')
  with patch('usb_export_ledger.atomic',side_effect=commit_then_interrupt):
   with self.assertRaises(OSError):self.commit()
  path=self.commit()
  self.assertTrue(path.is_file())
  self.assertTrue(validate_credential(self.base,self.source,self.manifest,'USB',destination='take')['qualified'])
 def test_pending_batch_can_be_exported_again_after_qc_changes(self):
  self.meta.update(status='pending_qc',qualified=False,retention_kind='pending_qc',qc_state='pending',reference_state='pending_qc')
  first=self.commit()
  self.meta.update(status='success',qualified=True,retention_kind='successful',qc_state='completed',reference_state='collected')
  self.tx='b'*32;self.receipt=self.receipt.parent/(self.tx+'.json')
  atomic(self.receipt,dict(state='verified',source=str(self.source),usb_uuid='USB',destination='take-new',manifest=self.manifest,transaction_id=self.tx,manifest_file='.headring-manifest-'+self.tx+'.json'))
  second=self.commit();self.assertNotEqual(first,second);self.assertTrue(first.exists());self.assertTrue(second.exists())
  self.assertTrue(validate_credential(self.base,self.source,self.manifest,'USB',destination='take-new')['qualified'])
 def test_changed_source_never_commits(self):
  (self.source/'data').write_bytes(b'changed')
  with self.assertRaises(CloudError):self.commit()
 def test_different_disk_never_matches(self):
  self.commit()
  with self.assertRaises(CloudError):validate_credential(self.base,self.source,self.manifest,'OTHER',destination='take',for_cleanup=True)
