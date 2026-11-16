import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
from cloud_common import CloudError
import usb_export_identity as identity

class IdentityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.device=self.root.stat().st_dev
  self.mount={42:(self.device,str(self.root),['rw'])}
  for obj,name,value in [(identity,'_mount_id',42),(identity,'_uuid','ABC')]:
   p=patch.object(obj,name,return_value=value);p.start();self.addCleanup(p.stop)
  p=patch.object(identity,'_mounts',side_effect=lambda:self.mount);p.start();self.addCleanup(p.stop)
 def test_open_relative_and_reject_traversal(self):
  with identity.UsbIdentity(self.root,'abc') as usb:
   fd=usb.open_file('file',os.O_WRONLY|os.O_CREAT|os.O_EXCL);os.write(fd,b'ok');os.close(fd)
   with self.assertRaises(CloudError):usb.open_file('../outside',os.O_WRONLY|os.O_CREAT)
  self.assertEqual((self.root/'file').read_bytes(),b'ok')
 def test_remount_or_device_change_rejected(self):
  with identity.UsbIdentity(self.root,'abc') as usb:
   self.mount.clear()
   with self.assertRaises(CloudError):usb.check()
   with self.assertRaises(CloudError):usb.open_file('file',os.O_WRONLY|os.O_CREAT)
 def test_symlink_parent_rejected(self):
  (self.root/'link').symlink_to('/tmp')
  with identity.UsbIdentity(self.root,'abc') as usb:
   with self.assertRaises(OSError):usb.open_file('link/file',os.O_WRONLY|os.O_CREAT)
 def test_second_mount_of_same_device_is_rejected(self):
  with identity.UsbIdentity(self.root,'abc') as usb:
   self.mount[43]=(self.device,'/somewhere-else',['rw'])
   with self.assertRaises(CloudError):usb.check()
 def test_nested_mount_rejected(self):
  sub=self.root/'nested';sub.mkdir()
  with identity.UsbIdentity(self.root,'abc') as usb:
   nested_inode=sub.stat().st_ino
   with patch.object(identity,'_mount_id',side_effect=lambda fd: 43 if os.fstat(fd).st_ino==nested_inode else 42):
    with self.assertRaises(CloudError):usb.open_dir('nested')
 def test_wrong_uuid_rejected(self):
  with self.assertRaises(CloudError):identity.UsbIdentity(self.root,'different')
