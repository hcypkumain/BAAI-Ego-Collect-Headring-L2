import importlib.util,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
HERE=Path(__file__).resolve().parents[1];spec=importlib.util.spec_from_file_location('integrated_deployer',HERE/'deploy_bundle.py');d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
class Helpers(unittest.TestCase):
 def setUp(self):self.files={row[0]:('content-'+row[0]).encode() for row in d.FEATURE_HELPERS.values()}
 def test_sudoers_allow_only_fixed_no_argument_helpers(self):
  r=d.feature_files(self.files,'orangepi');self.assertIn(b'mount-readonly ""',r['/etc/sudoers.d/collector-usb-export']['data']);self.assertIn(b'mount-export ""',r['/etc/sudoers.d/collector-usb-export']['data']);self.assertEqual(r['/etc/sudoers.d/headring-phone-time']['data'],b'orangepi ALL=(root) NOPASSWD: /usr/local/sbin/headring-phone-time ""\n')
  with self.assertRaises(RuntimeError):d.feature_files(self.files,'root, ALL')
 def test_required_helpers_cannot_be_missing(self):
  del self.files['support/headring-phone-time']
  with self.assertRaisesRegex(RuntimeError,'missing'):d.feature_files(self.files,'orangepi')
 def test_unrecognized_or_symlink_target_refuses(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'helper';p.write_bytes(b'unknown')
   entries={str(p):{'data':b'new','mode':0o755,'known':set()}}
   with patch.object(d,'feature_files',return_value=entries),patch.object(d,'cmd',return_value=SimpleNamespace(stdout='orangepi')):
    with self.assertRaises(RuntimeError):d.plan_feature_helpers({'integrated_helpers_version':1},self.files)
    p.unlink();p.symlink_to(Path(t)/'external')
    with self.assertRaises(RuntimeError):d.plan_feature_helpers({'integrated_helpers_version':1},self.files)
 def test_feature_gate_is_backward_compatible(self):
  self.assertEqual(d.plan_feature_helpers({},{}),{})
  with self.assertRaises(RuntimeError):d.plan_feature_helpers({'integrated_helpers_version':99},{})
if __name__=='__main__':unittest.main()
