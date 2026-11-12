import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

path=Path(__file__).resolve().parents[1]/'resources/remote_components.py'
spec=importlib.util.spec_from_file_location('components_root',path)
component=importlib.util.module_from_spec(spec); spec.loader.exec_module(component)

class ComponentsRootTests(unittest.TestCase):
    def setup_files(self, root):
        files={'a':('/usr/local/lib/collector-usb-task-import/mount-readonly',0o755),'b':('/etc/component',0o644)}
        payload={'a':b'new-helper','b':b'new-data'}
        expected={dest:{'type':'absent'} for dest,_ in files.values()}
        return files,payload,expected

    def test_temp_install_umask_idempotent_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root'; root.mkdir(mode=0o700); files,payload,expected=self.setup_files(root)
            old=os.umask(0o077)
            try:
                with mock.patch.object(component,'FILES',files):
                    r=component.apply_files(root,payload,expected,Path(d)/'backup',owner=os.getuid(),group=os.getgid(),root_group_only=False)
                    self.assertEqual(r['status'],'installed')
                    parent=root/'usr/local/lib/collector-usb-task-import'
                    self.assertEqual(parent.stat().st_mode&0o777,0o755)
                    now={dest:component.state(root/dest.lstrip('/')) for dest,_ in files.values()}
                    with mock.patch.object(component,'atomic_write',side_effect=AssertionError('unexpected write')):
                        r=component.apply_files(root,payload,now,Path(d)/'backup2',owner=os.getuid(),group=os.getgid(),root_group_only=False)
                    self.assertEqual(r['status'],'already_current'); self.assertFalse((Path(d)/'backup2').exists())
            finally: os.umask(old)

    def test_failure_rollback_and_external_edit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root'; root.mkdir(mode=0o700); files,payload,expected=self.setup_files(root)
            with mock.patch.object(component,'FILES',files):
                def fail(source,path): raise RuntimeError('injected failure')
                with self.assertRaisesRegex(RuntimeError,'injected failure'):
                    component.apply_files(root,payload,expected,Path(d)/'backup',owner=os.getuid(),group=os.getgid(),after=fail,root_group_only=False)
                self.assertFalse((root/'usr/local/lib/collector-usb-task-import/mount-readonly').exists())
                def external(source,path): path.write_bytes(b'outside'); raise RuntimeError('injected')
                with self.assertRaisesRegex(RuntimeError,'rollback_incomplete'):
                    component.apply_files(root,payload,expected,Path(d)/'backup2',owner=os.getuid(),group=os.getgid(),after=external,root_group_only=False)
                self.assertEqual((root/'usr/local/lib/collector-usb-task-import/mount-readonly').read_bytes(),b'outside')

    def test_dropin_conflict(self):
        with mock.patch.object(component,'run',return_value='FragmentPath=/etc/systemd/system/collector-usb-task-mount.service\nDropInPaths=/etc/systemd/system/override.conf'):
            with self.assertRaisesRegex(RuntimeError,'conflict'): component.unit_conflicts()

if __name__=='__main__': unittest.main()
