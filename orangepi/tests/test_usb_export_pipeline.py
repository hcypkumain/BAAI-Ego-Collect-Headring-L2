import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src/orangepi_collect'/name) for name in ('control','common')]
import usb_export_identity as identity
import usb_export_pipeline as pipeline
from usb_export_ledger import commit_credential

class PipelineTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.usbroot=self.root/'usb';self.usbroot.mkdir()
  for p in [patch.object(identity,'_mount_id',return_value=42),patch.object(identity,'_uuid',return_value='USB'),patch.object(identity,'_mounts',return_value={42:(self.usbroot.stat().st_dev,str(self.usbroot),['rw'])})]:p.start();self.addCleanup(p.stop)
  self.usb=identity.UsbIdentity(self.usbroot,'USB');self.addCleanup(self.usb.close)
  self.stores={};self.items=[]
  for index,mode in enumerate(('emg','eeg')):
   base=self.root/mode/'orangepi';source=self.root/'sd'/mode/'data'/'shift'/('take-'+mode);source.mkdir(parents=True)
   (source/'data').write_bytes(mode.encode());project=base.parent;image=project/'configs/environments/image.png';image.parent.mkdir(parents=True);image.write_bytes(b'image')
   pending=mode=='eeg';status='pending_qc' if pending else 'success'
   meta=dict(id=source.name,token='token',status=status,qualified=not pending,retention_kind='pending_qc' if pending else 'successful',reference_path='configs/environments/image.png',qc_state='pending' if pending else 'completed',reference_state='pending_qc' if pending else 'collected')
   snapshot=dict(id=source.name,status=status,qualified=not pending,retained=True,accepted_at_capture=not pending,has_data=True,start=1,duration=1)
   store=SimpleNamespace(base=base,project=project,roots=lambda s=source:(s.parent,s.parent.parent.parent),metadata=lambda p,m=meta:m,export_snapshot=lambda p,m,s=snapshot:s)
   self.stores[(mode,str(source))]=store
   self.items.append(dict(mode=mode,source=str(source),transaction_id=str(index+1)*32))
  self.pipeline=pipeline.ExportPipeline(self.root/'code',self.usb,store_factory=lambda m,s:self.stores[(m,s)])
 def test_service_restart_and_mount_lease(self):
  from usb_export_service import ExportService
  class Lease:
   def __enter__(inner):return self.usb
   def __exit__(inner,*args):return False
  def pending(_):
   value=self.pipeline.job.status()
   return [{'path':str(self.pipeline.job.path)}] if value and value['state']!='complete' else []
  service=ExportService(self.root/'code',lease_factory=Lease,discover_fn=lambda:self.items,pipeline_factory=lambda base,usb:self.pipeline)
  calls=[0]
  def fail_once(*args):
   result=commit_credential(*args);calls[0]+=1
   if calls[0]==1:raise OSError('power loss')
   return result
  with patch('usb_export_service.pending_exports',side_effect=pending),patch.object(pipeline,'commit_credential',side_effect=fail_once):
   service.start();service.thread.join(3)
   self.assertEqual(service.status()['state'],'committed');self.assertIn('power loss',service.status()['error'])
   service.start(recover=True);service.thread.join(3)
  self.assertEqual(service.status()['state'],'complete');self.assertTrue(service.status()['unmounted'])
 def test_swap_disk_cannot_resume_or_clean(self):
  self.pipeline.create(self.items);self.usb.uuid='OTHER'
  with self.assertRaises(ValueError):self.pipeline.run()
  self.assertTrue(all(Path(item['source']).exists() for item in self.items))
 def test_two_modes_resume_after_credential_commit(self):
  self.pipeline.create(self.items);calls=[0]
  def fail_once(*args):
   result=commit_credential(*args);calls[0]+=1
   if calls[0]==1:raise OSError('power loss')
   return result
  with patch.object(pipeline,'commit_credential',side_effect=fail_once):
   with self.assertRaises(OSError):self.pipeline.run()
  self.assertEqual(self.pipeline.job.status()['state'],'committed')
  self.assertEqual(self.pipeline.run()['state'],'complete')
  self.assertFalse(Path(self.items[0]['source']).exists())
  self.assertTrue(Path(self.items[1]['source']).exists())
  result=self.pipeline.job.status()['results']
  self.assertEqual(result[self.items[1]['transaction_id']]['cleanup'],'retained_pending_qc')
