"""Fresh Windows/Linux bundle sources must not bypass the data-host initializer."""
import ast
from pathlib import Path
import unittest
import os
import importlib.util
import base64
import shlex
import tempfile
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[2]

class ProvisionTests(unittest.TestCase):
    def test_large_manifest_is_streamed_instead_of_shell_argument(self):
        tree=ast.parse((ROOT/'tools/provision/install.py').read_text())
        function=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='embedded')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'device_setup.py').write_text('# trusted backend')
            env={'base64':base64,'ROOT':root,'BASE':'/station/orangepi','shlex':shlex}
            exec(compile(ast.Module(body=[function],type_ignores=[]),'streamed_installer','exec'),env)
            stdin=Mock();out=Mock();err=Mock();out.read.return_value=b'OK';err.read.return_value=b'';out.channel.recv_exit_status.return_value=0
            client=Mock();client.exec_command.return_value=(stdin,out,err)
            self.assertEqual(env['embedded'](client,"request='"+'x'*200000+"'"),'OK')
            command=client.exec_command.call_args.args[0]
            self.assertLess(len(command),100);self.assertTrue(command.endswith(' -'))
            self.assertGreater(len(stdin.write.call_args.args[0]),200000)
            stdin.channel.shutdown_write.assert_called_once()

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
