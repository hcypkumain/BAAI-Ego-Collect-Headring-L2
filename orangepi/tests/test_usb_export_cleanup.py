import sys,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
from cloud_common import CloudError,source_manifest,atomic
from usb_export_cleanup import cleanup
from usb_export_ledger import commit_credential,credential_path
import usb_export_identity as identity

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);r=Path(self.tmp.name)
  self.base=r/'base';self.base.mkdir();self.source=r/'sd'/'take';self.source.mkdir(parents=True);self.usbroot=r/'usb';self.usbroot.mkdir();self.dest=self.usbroot/'take';self.dest.mkdir()
  (self.source/'data').write_bytes(b'abc');(self.dest/'data').write_bytes(b'abc');self.manifest=source_manifest(self.source)
  patches=[patch.object(identity,'_mount_id',return_value=42),patch.object(identity,'_uuid',return_value='USB'),patch.object(identity,'_mounts',return_value={42:(self.usbroot.stat().st_dev,str(self.usbroot),['rw'])})]
  for p in patches:p.start();self.addCleanup(p.stop)
  self.usb=identity.UsbIdentity(self.usbroot,'USB');self.addCleanup(self.usb.close)
  self.project=r/'project'; self.image=self.project/'configs/environments/image.png';self.image.parent.mkdir(parents=True);self.image.write_bytes(b'image')
  meta=dict(id='take',token='token',status='success',qualified=True,retention_kind='successful',reference_path='configs/environments/image.png',qc_state='completed',reference_state='collected')
  snapshot=dict(id='take',status='success',qualified=True,retained=True,accepted_at_capture=True,has_data=True,start=1,duration=1)
  daily=patch('daily_collection.take_snapshot',return_value=snapshot);daily.start();self.addCleanup(daily.stop)
  self.store=SimpleNamespace(project=self.project,operations=self.base/'runtime/recording_operations',roots=lambda:(self.source.parent,self.source.parent),metadata=lambda source:meta)
  store=self.store
  tx='a'*32;receipt=self.base/'runtime/usb_exports/receipts'/(tx+'.json')
  from usb_export_copy import _ensure_manifest
  _ensure_manifest(self.usb,self.source,'take',self.manifest,tx)
  atomic(receipt,dict(state='verified',source=str(self.source),usb_uuid='USB',destination='take',manifest=self.manifest,transaction_id=tx,manifest_file='.headring-manifest-'+tx+'.json'))
  commit_credential(self.base,store,self.source,receipt,self.usb)
 def run_cleanup(self):return cleanup(self.base,self.source,'take',self.manifest,self.usb)
 def test_verified_cleanup_and_replay(self):
  self.assertEqual(self.run_cleanup()['state'],'complete');self.assertFalse(self.source.exists());self.assertEqual(self.run_cleanup()['state'],'complete')
 def test_completed_evidence_preserves_reference_and_daily_record(self):
  self.run_cleanup()
  sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/storage'))
  sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
  import ledger
  with patch.object(ledger,'BASE',self.base),patch.object(ledger,'PROJECT',self.project),patch.object(ledger,'_uploaded_completion',return_value=False),patch.object(ledger,'_server_handoff_completion',return_value=False):
   self.assertTrue(ledger.valid_used(dict(session=str(self.source),status='success'),self.image))
   self.image.write_bytes(b'changed')
   self.assertFalse(ledger.valid_used(dict(session=str(self.source),status='success'),self.image))
  from daily_collection import DailyCollection
  daily=DailyCollection(self.base,self.store,lambda:dict(state='idle',privacy={'required':False}),autostart=False)
  self.assertTrue(daily.refresh());self.assertTrue(daily.records['take']['qualified']);self.assertTrue(daily.records['take']['local_cleaned'])
 def test_missing_credential_or_corrupt_destination_keeps_source(self):
  credential_path(self.base,self.source,'take').unlink()
  with self.assertRaises(CloudError):self.run_cleanup()
  self.assertTrue(self.source.exists())
 def test_unplug_during_purge_keeps_remaining_tree_for_recovery(self):
  import usb_export_cleanup as module
  original=module._purge
  def disconnect(path,manifest,usb):
   with patch.object(usb,'check',side_effect=CloudError('U盘已拔出')):
    return original(path,manifest,usb)
  with patch.object(module,'_purge',side_effect=disconnect):
   with self.assertRaises(CloudError):self.run_cleanup()
  self.assertTrue((self.source.parent/'.usb-export-cleanup'/'take'/'data').exists())
  self.assertEqual(self.run_cleanup()['state'],'complete')
 def test_interrupted_purge_resumes_after_destination_reverified(self):
  def interrupt(path,manifest,usb):
   (Path(path)/'data').unlink();raise OSError('power loss')
  with patch('usb_export_cleanup._purge',side_effect=interrupt):
   with self.assertRaises(OSError):self.run_cleanup()
  (self.dest/'data').write_bytes(b'bad')
  with self.assertRaises(CloudError):self.run_cleanup()
  (self.dest/'data').write_bytes(b'abc')
  self.assertEqual(self.run_cleanup()['state'],'complete')
