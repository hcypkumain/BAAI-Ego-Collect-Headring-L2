import importlib.util,tempfile,unittest,types
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('installer',ROOT/'scripts/install_pi_services.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
class Checks(unittest.TestCase):
 def test_rule_observes_without_run_or_write(self):
  rule=(ROOT/'deployment/udev/83-typec.rules').read_text()
  self.assertIn('remove|change',rule);self.assertNotIn('RUN',rule);self.assertNotIn('usbdevice',rule);self.assertNotIn('/mode',rule)
 def test_install_includes_legacy_rule_replacement_and_backup(self):
  ops=installer.plan(ROOT,False);self.assertIn((ROOT/'deployment/udev/83-typec.rules',Path('/etc/udev/rules.d/83-typec.rules'),False),ops)
  self.assertIn((ROOT/'deployment/udev/61-usbdevice.rules',Path('/etc/udev/rules.d/61-usbdevice.rules'),False),ops)
  text=(ROOT/'scripts/install_pi_services.py').read_text();self.assertIn("legacy=Path('/usr/local/bin/typec_usb2.sh')",text);self.assertNotIn("'trigger'",text)
 def test_other_matching_rules_stop_install(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp);(d/'83-typec.rules').write_text('RUN+="/usr/bin/usbdevice restart"')
   self.assertEqual(installer.unsafe_typec_rules([d]),[str(d/'83-typec.rules')])
   self.assertEqual(installer.unsafe_typec_rules([d],{'83-typec.rules':ROOT/'deployment/udev/83-typec.rules'}),[])
   unsafe=d/'84-another.rules';unsafe.write_text('ACTION=="change", RUN+="/usr/bin/usbdevice restart"')
   self.assertEqual(installer.unsafe_typec_rules([d],{'83-typec.rules':ROOT/'deployment/udev/83-typec.rules'}),[str(unsafe)])
   unsafe.write_text('# RUN+="/usr/bin/usbdevice restart"\n');self.assertEqual(installer.unsafe_typec_rules([d],{'83-typec.rules':ROOT/'deployment/udev/83-typec.rules'}),[])
 def test_effective_precedence_and_masks(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);etc=root/'etc';run=root/'run';vendor=root/'vendor';alias=root/'alias'
   for d in (etc,run,vendor,alias):d.mkdir()
   rule=vendor/'61-usbdevice.rules';rule.write_text('SUBSYSTEM=="android_usb", RUN+="/usr/bin/usbdevice update"')
   dirs=[etc,run,vendor,alias]
   self.assertEqual(installer.unsafe_typec_rules(dirs),[str(rule)])
   (etc/'61-usbdevice.rules').symlink_to('/dev/null');self.assertEqual(installer.unsafe_typec_rules(dirs),[])
   (etc/'61-usbdevice.rules').unlink();(etc/'61-usbdevice.rules').write_text('# no automatic action\n');self.assertEqual(installer.unsafe_typec_rules(dirs),[])
   (etc/'61-usbdevice.rules').write_text('SUBSYSTEM=="typec", PROGRAM="/bin/true"');self.assertEqual(installer.unsafe_typec_rules(dirs),[str(etc/'61-usbdevice.rules')])
   (etc/'61-usbdevice.rules').unlink();(run/'61-usbdevice.rules').write_text('# safe runtime override\n');self.assertEqual(installer.unsafe_typec_rules(dirs),[])
   (vendor/'62-other.rules').write_text('RUN+="/usr/bin/usbdevice update"');self.assertEqual(installer.unsafe_typec_rules(dirs),[str(vendor/'62-other.rules')])
 def test_typec_run_braces_are_detected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'62-test.rules'
   for subsystem in ('typec','android_usb'):
    for action in ('RUN{program}+="/tmp/x"','RUN{builtin}+="other"','PROGRAM=="/tmp/x"','IMPORT{program}="/tmp/x"'):
     with self.subTest(subsystem=subsystem,action=action):
      p.write_text('SUBSYSTEM=="'+subsystem+'", '+action+'\n')
      self.assertEqual(installer.unsafe_typec_rules([p.parent]),[str(p)])
 def test_broken_link_and_unreadable_rule_are_not_assumed_safe(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp);p=d/'61-usbdevice.rules';p.symlink_to(d/'missing')
   with self.assertRaisesRegex(RuntimeError,'Cannot inspect'):installer.unsafe_typec_rules([d])
   p.unlink();p.write_text('safe')
   with patch.object(Path,'read_text',side_effect=PermissionError('denied')):
    with self.assertRaisesRegex(RuntimeError,'Cannot inspect'):installer.unsafe_typec_rules([d])
 def test_unreadable_or_disappearing_directory_is_not_skipped(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=Path(tmp)
   for error in (PermissionError('denied'),FileNotFoundError('removed after stat')):
    with self.subTest(error=error),patch.object(installer.os,'scandir',side_effect=error):
     with self.assertRaisesRegex(RuntimeError,'Cannot inspect udev directory'):installer.unsafe_typec_rules([d])
 def test_existing_or_broken_target_symlink_preflight_has_no_writes(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);script=root/'orangepi/scripts/run.sh';script.parent.mkdir(parents=True);script.write_text('ok')
   source=root/'source';source.write_text('candidate');sentinel=root/'already';sentinel.write_text('unchanged')
   target=root/'61-usbdevice.rules';ops=[(source,sentinel,False),(source,target,False)]
   spec=types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda _:None))
   module=types.SimpleNamespace(CaptureGuard=lambda _:types.SimpleNamespace(allowed=lambda:(True,'idle')))
   for link in ('/dev/null',str(root/'missing')):
    target.symlink_to(link)
    with patch('sys.argv',['install','--apply','--project-root',str(root)]), \
         patch.object(installer,'plan',return_value=ops),patch.object(installer.platform,'machine',return_value='aarch64'), \
         patch.object(installer.os,'geteuid',return_value=0),patch.object(installer,'usb_adb',return_value=source), \
         patch.object(installer.importlib.util,'spec_from_file_location',return_value=spec), \
         patch.object(installer.importlib.util,'module_from_spec',return_value=module), \
         patch.object(installer.subprocess,'run'),patch.object(installer,'unsafe_typec_rules',return_value=[]), \
         patch.object(installer.shutil,'copy2') as copy,patch.object(installer.tempfile,'mkstemp') as create:
     with self.assertRaisesRegex(RuntimeError,'symlink'):installer.main()
     copy.assert_not_called();create.assert_not_called();self.assertEqual(sentinel.read_text(),'unchanged')
    target.unlink()
if __name__=='__main__':unittest.main()
