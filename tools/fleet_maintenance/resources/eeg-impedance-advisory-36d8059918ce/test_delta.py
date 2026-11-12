import importlib.util, pathlib, tempfile, shutil, unittest, json
from unittest.mock import patch
here=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('delta',here/'transaction.py'); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class DeltaTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
  self.project=self.root/'project';self.project.mkdir();self.patch=patch.object(m,'PROJECT',self.project);self.patch.start();self.addCleanup(self.patch.stop)
  pin=pathlib.Path('/tmp/opencode/8788-eeg-pin')
  for item in m.PINNED_FILES:
   p=self.project/item['path'];p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(pin/item['path'],p)
  p=self.project/'profiles/eeg/orangepi';p.mkdir(parents=True);(p/'src').symlink_to(self.project/'orangepi/src',target_is_directory=True)
  (p/'configs').mkdir();(p/'configs/devices.yaml').write_text('eeg: {}\n')
 def test_exact_five_and_baseline(self):
  self.assertEqual(len(m.delta_inventory()),5)
  p=self.project/m.PINNED_FILES[0]['path'];p.write_text('drift')
  with self.assertRaises(m.Abort):m.delta_inventory()
 def test_shadow_and_strict_refused(self):
  conf=self.project/'profiles/eeg/orangepi/configs/devices.yaml';conf.write_text('eeg:\n  impedance_acceptance_policy: strict\n')
  with self.assertRaises(m.Abort):m.delta_inventory()
  conf.write_text('eeg: {}\n');p=self.project/'profiles/eeg/orangepi/src';p.unlink();p.mkdir()
  with self.assertRaises(m.Abort):m.delta_inventory()
 def test_active_refused(self):
  with self.assertRaises(m.Abort):m.assert_idle({'state':'recording','busy':False,'session':None})
 def test_corrupt_backup_does_not_overwrite(self):
  item=m.PINNED_FILES[0];rel=item['path'];p=self.project/rel;p.write_bytes(b'new');backup=self.root/'backup';b=backup/'files'/rel;b.parent.mkdir(parents=True);b.write_bytes(b'corrupt')
  meta={rel:{'exists':True,'sha256':'wrong','uid':0,'gid':0,'mode':420}}
  errors=m.rollback([rel],backup,meta,{rel:m.digest(p)})
  self.assertTrue(errors);self.assertEqual(p.read_bytes(),b'new')
 def test_payload_and_manifest_exact(self):
  with tempfile.TemporaryDirectory() as d:
   import zipfile
   with zipfile.ZipFile(here/'bundle.zip') as z:z.extractall(d)
   with patch.object(m,'require_root_private_tree'), patch.object(m,'PROJECT',pathlib.Path('/home/orangepi/Desktop/Embodied-Brain-Collect-main.2')):
    self.assertEqual(len(m.delta_bundle(pathlib.Path(d))['files']),5)
    p=pathlib.Path(d)/'payload'/m.PINNED_FILES[0]['path'];p.write_bytes(b'bad')
    with self.assertRaises(m.Abort):m.delta_bundle(pathlib.Path(d))
if __name__=='__main__':unittest.main()
