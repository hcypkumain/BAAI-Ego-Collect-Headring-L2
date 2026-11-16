import importlib.util
import json
import io
import fcntl
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

script = Path(__file__).resolve().parents[1] / 'scripts/usb_export_mount.py'
spec = importlib.util.spec_from_file_location('usb_export_mount', script)
mount = importlib.util.module_from_spec(spec); spec.loader.exec_module(mount)


class MountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'mounts'; self.root.mkdir()
        self.device = '/dev/sda1'
        self.row = {'path': self.device, 'fstype': 'exfat', 'type': 'part', 'uuid': 'TEST-UUID', 'mountpoint': None}
        self.info = SimpleNamespace(st_rdev=os.makedev(8, 1))
        self.calls = []

    def run_helper(self, candidates=None, command_return=0, safe=True, mount_device=None):
        def output(args, **kwargs):
            if 'lsblk' in args[0]:
                self.assertIn('PATH,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT', args)
                return b'{"blockdevices":[]}'
            phase='rw' if self.calls else 'ro'
            return json.dumps({'filesystems': [{'target':str(self.root/'8-1'),'source': self.device, 'uuid': 'TEST-UUID', 'options': phase+',nodev,nosuid'}]}).encode()
        def command(args, **kwargs):
            self.calls.append(args); return SimpleNamespace(returncode=command_return)
        with patch.object(mount, 'ROOT', self.root), patch.object(mount.os, 'geteuid', return_value=0), \
             patch.object(mount.sys, 'argv', ['helper']), patch.object(mount, 'safe_directory', return_value=safe), patch.object(mount, 'mount_device', return_value=self.info.st_rdev if mount_device is None else mount_device), patch.object(mount, 'candidates', return_value=candidates if candidates is not None else [(self.row, self.info)]), patch.object(mount, 'mount_id', return_value=42), patch.object(mount, 'mount_device_count', return_value=1), \
             patch.object(mount.subprocess, 'check_output', side_effect=output), patch.object(mount.subprocess, 'run', side_effect=command), \
             patch.object(mount.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)), patch.dict(mount.os.environ, {'SUDO_USER':'collector'}):
            mount.mount_locked()

    def test_multiple_volumes_rejected(self):
        with self.assertRaises(RuntimeError): self.run_helper([(self.row, self.info), (self.row, self.info)])
        self.assertFalse(self.calls)

    def test_volume_mounted_elsewhere_is_rejected(self):
        self.row['mountpoint'] = '/media/other'
        with self.assertRaises(RuntimeError): self.run_helper()
        self.assertFalse(self.calls)

    def test_shared_lock_refuses_simultaneous_mount(self):
        fd = os.open(str(self.root/'.mount.lock'), os.O_RDWR|os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd);fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG|0o600)
        with patch.object(mount,'ROOT',self.root), patch.object(mount.os,'geteuid',return_value=0), patch.object(mount.os,'fstat',return_value=info), patch.object(mount,'safe_directory',return_value=True), patch.object(mount.sys,'argv',['helper']):
            with self.assertRaises(RuntimeError):mount.main()

    def test_import_operation_lock_blocks_export(self):
        fd=os.open(str(self.root/'.operation.lock'),os.O_RDWR|os.O_CREAT,0o666)
        self.addCleanup(os.close,fd);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o600)
        with patch.object(mount,'ROOT',self.root),patch.object(mount.os,'geteuid',return_value=0),patch.object(mount.os,'fstat',return_value=info),patch.object(mount,'safe_directory',return_value=True),patch.object(mount.sys,'argv',['helper']),patch.object(mount,'mount_locked') as mounted:
            with self.assertRaises(RuntimeError):mount.main()
            mounted.assert_not_called()

    def test_lock_held_until_owner_releases(self):
        info=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG|0o600)
        def verify(mounted):
            op=os.open(str(self.root/'.operation.lock'),os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(op,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(op)
            fd=os.open(str(self.root/'.mount.lock'),os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
        with patch.object(mount,'ROOT',self.root), patch.object(mount.os,'geteuid',return_value=0), patch.object(mount.os,'fstat',return_value=info), patch.object(mount,'safe_directory',return_value=True), patch.object(mount.sys,'argv',['helper']), patch.object(mount.sys,'stdin',io.StringIO('release\n')), patch.object(mount,'mount_locked',return_value={'uuid':'USB'}), patch.object(mount,'safe_unmount',side_effect=verify):
            mount.main()

    def test_unmount_rejects_changed_identity(self):
        mounted={'device':self.device,'mountpoint':'/run/test','uuid':'USB','major':8,'minor':1}
        response=json.dumps({'filesystems':[{'source':self.device,'target':'/run/test','uuid':'OTHER'}]}).encode()
        info=SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1))
        with patch.object(mount.subprocess,'check_output',return_value=response),patch.object(mount.os,'stat',return_value=info),patch.object(mount.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):mount.safe_unmount(mounted)
            run.assert_not_called()

    def test_existing_dedicated_readonly_mount_is_verified_and_remounted(self):
        self.row['mountpoint']=str(self.root/'8-1')
        self.run_helper(safe=False)
        self.assertIn('remount,rw,nodev,nosuid,noexec,uid=1000,gid=1000,umask=022',self.calls[0])

    def test_autofs_import_mount_is_stopped_before_writable_export(self):
        self.row['mountpoint']=str(self.root/'8-1');target=self.row['mountpoint']
        layer={'target':target,'source':'systemd-1','uuid':None,'options':'rw','fstype':'autofs'}
        ro={'target':target,'source':self.device,'uuid':'TEST-UUID','options':'ro,nodev,nosuid','fstype':'exfat'}
        rw=dict(ro,options='rw,nodev,nosuid')
        responses=[b'{"blockdevices":[]}',json.dumps({'filesystems':[layer,ro]}).encode(),json.dumps({'filesystems':[rw]}).encode()]
        def command(args,**kwargs):
            self.calls.append(args)
            return SimpleNamespace(returncode=1 if args[0].endswith('findmnt') else 0)
        with patch.object(mount,'ROOT',self.root),patch.object(mount,'candidates',return_value=[(self.row,self.info)]),patch.object(mount,'mount_device',return_value=self.info.st_rdev),patch.object(mount,'mount_device_count',side_effect=[0,1]),patch.object(mount,'mount_id',side_effect=[42,43]),patch.object(mount.subprocess,'check_output',side_effect=responses),patch.object(mount.subprocess,'run',side_effect=command),patch.object(mount.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=1000,pw_gid=1000)),patch.dict(mount.os.environ,{'SUDO_USER':'collector'}):
            result=mount.mount_locked()
        self.assertEqual(result['mount_id'],43)
        self.assertEqual(self.calls[0],['/usr/bin/systemd-umount','--no-ask-password',target])
        self.assertEqual(self.calls[1],['/usr/bin/findmnt','-n','-M',target])
        self.assertIn('-t',self.calls[2])

    def test_untrusted_autofs_layer_is_rejected_before_unmount(self):
        self.row['mountpoint']=str(self.root/'8-1');target=self.row['mountpoint']
        rows=[{'target':target,'source':'other','fstype':'autofs'}, {'target':target,'source':self.device,'uuid':'TEST-UUID','options':'ro','fstype':'exfat'}]
        with patch.object(mount,'ROOT',self.root),patch.object(mount,'candidates',return_value=[(self.row,self.info)]),patch.object(mount.subprocess,'check_output',side_effect=[b'{"blockdevices":[]}',json.dumps({'filesystems':rows}).encode()]),patch.object(mount.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):mount.mount_locked()
            run.assert_not_called()

    def test_existing_mount_wrong_device_is_rejected(self):
        self.row['mountpoint']=str(self.root/'8-1')
        with self.assertRaises(RuntimeError):self.run_helper(mount_device=os.makedev(8,2))
        self.assertFalse(self.calls)

    def test_mount_failure_is_reported(self):
        with self.assertRaises(RuntimeError): self.run_helper(command_return=1)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any('format' in str(x) for x in self.calls))

if __name__ == '__main__': unittest.main()
