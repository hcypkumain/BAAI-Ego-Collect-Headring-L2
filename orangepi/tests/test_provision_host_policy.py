"""Fresh Windows/Linux bundle sources must not bypass the data-host initializer."""
import ast
from pathlib import Path
import unittest
import os
import importlib.util

ROOT=Path(__file__).resolve().parents[2]

class ProvisionTests(unittest.TestCase):
    def test_controller_uses_current_unified_release(self):
        tree=ast.parse((ROOT/'tools/provision/install.py').read_text())
        version=next(node.value.value for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets))
        self.assertEqual(version,'headring-unified-20261107-v3')

    def test_fresh_activation_requires_host_before_starting_collector(self):
        text=(ROOT/'tools/provision/device_setup.py').read_text()
        host=text.index("host = json.loads(run(['/usr/local/sbin/headring-usb-data-host'")
        activation=text.index("run(['systemctl', 'start', 'easytier', 'pi-phone-media', 'embodied-pi-collect', 'usb-phone-link']")
        self.assertLess(host,activation)
        self.assertIn("host.get('state') != 'host'",text)
        self.assertIn("'headring-usb-data-host'",text)

    def test_extract_allowlist_keeps_identity_and_keys_out_of_payload(self):
        text=(ROOT/'tools/provision/device_setup.py').read_text()
        self.assertIn("'usr/local/sbin/headring-usb-data-host'",text)
        self.assertIn("'home/orangepi/maintenance/usb-phone-adb/keys'",text)
        self.assertNotIn("'opt/egorecord/calibrations/'",text)

    @unittest.skipUnless(os.name=='posix','Backend imports Linux platform modules')
    def test_actual_archive_allowlist_accepts_only_reviewed_host_paths(self):
        path=ROOT/'tools/provision/device_setup.py'
        spec=importlib.util.spec_from_file_location('host_provision_backend',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for name in ['usr/local/sbin/headring-usb-data-host','etc/systemd/system/headring-usb-data-host.service','etc/systemd/system/camd.service.d/95-usb-data-host.conf']:
            self.assertTrue(module.archive_allowed(name),name)
        for name in ['../etc/shadow','/etc/shadow','opt/egorecord/DEVICE_ID','opt/egorecord/calibrations/camera.yaml','home/orangepi/maintenance/usb-phone-adb/keys/adbkey','home/orangepi/Desktop/Embodied-Brain-Collect-main.2/profiles/eeg/orangepi/runtime/controller.json']:
            self.assertFalse(module.archive_allowed(name),name)

if __name__=='__main__':unittest.main()
