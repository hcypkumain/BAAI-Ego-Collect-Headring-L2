import importlib.util,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('installer',ROOT/'scripts/install_pi_services.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
class Checks(unittest.TestCase):
 def test_rule_observes_without_run_or_write(self):
  rule=(ROOT/'deployment/udev/83-typec.rules').read_text()
  self.assertIn('remove|change',rule);self.assertNotIn('RUN',rule);self.assertNotIn('usbdevice',rule);self.assertNotIn('/mode',rule)
 def test_install_includes_legacy_rule_replacement_and_backup(self):
  ops=installer.plan(ROOT,False);self.assertIn((ROOT/'deployment/udev/83-typec.rules',Path('/etc/udev/rules.d/83-typec.rules'),False),ops)
  text=(ROOT/'scripts/install_pi_services.py').read_text();self.assertIn("legacy=Path('/usr/local/bin/typec_usb2.sh')",text);self.assertNotIn("'trigger'",text)
 def test_other_matching_rules_stop_install(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp);(d/'83-typec.rules').write_text('RUN+="/usr/bin/usbdevice restart"')
   self.assertEqual(installer.unsafe_typec_rules([d]),[])
   unsafe=d/'84-another.rules';unsafe.write_text('ACTION=="change", RUN+="/usr/bin/usbdevice restart"')
   self.assertEqual(installer.unsafe_typec_rules([d]),[str(unsafe)])
   unsafe.write_text('# RUN+="/usr/bin/usbdevice restart"\n');self.assertEqual(installer.unsafe_typec_rules([d]),[])
if __name__=='__main__':unittest.main()
