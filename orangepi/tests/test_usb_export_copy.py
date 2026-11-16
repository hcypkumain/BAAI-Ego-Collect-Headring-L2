import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control'))
from cloud_common import CloudError
import os
import usb_export_identity as identity
from usb_export_copy import copy_recording


class ExportCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.base, self.source, self.usb = root/'base', root/'recording', root/'usb'
        self.base.mkdir(); self.source.mkdir(); self.usb.mkdir()
        self.patches = [patch.object(identity, '_mount_id', return_value=42), patch.object(identity, '_uuid', return_value='TEST'), patch.object(identity, '_mounts', return_value={42: (self.usb.stat().st_dev, str(self.usb), ['rw'])})]
        for item in self.patches: item.start(); self.addCleanup(item.stop)
        self.guard = identity.UsbIdentity(self.usb, 'TEST'); self.addCleanup(self.guard.close)
        (self.source/'stream').mkdir()
        (self.source/'stream'/'data').write_bytes(b'example')

    @patch('usb_export_copy.os.sync')
    def test_copy_verified_and_original_retained(self, _):
        path, receipt = copy_recording(self.base, self.source, self.guard)
        self.assertTrue(path.exists())
        self.assertEqual((self.usb/'recording'/'stream'/'data').read_bytes(), b'example')
        self.assertTrue(self.source.exists())
        self.assertFalse(receipt['local_deleted'])

    @patch('usb_export_copy.os.sync')
    def test_unfinished_directory_is_not_exported(self, _):
        (self.source/'capture.partial').mkdir()
        with self.assertRaises(CloudError):copy_recording(self.base,self.source,self.guard)
        self.assertTrue(self.source.exists());self.assertEqual(list(self.usb.iterdir()),[])

    @patch('usb_export_copy.os.sync')
    def test_source_modified_during_copy_cannot_create_receipt(self, _):
        changed = [False]
        def progress(phase, done, total):
            if not changed[0]:
                (self.source/'stream'/'data').write_bytes(b'changed')
                changed[0] = True
        with self.assertRaises(CloudError):
            copy_recording(self.base, self.source, self.guard, progress)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.usb/'recording').exists())
        receipts = self.base/'runtime/usb_exports/receipts'
        self.assertFalse(receipts.exists())

    @patch('usb_export_copy.os.sync')
    def test_partial_copy_retries_without_accumulating_staging(self, _):
        transaction='b'*32
        def interrupt(phase,done,total):
            if phase=='copying':raise OSError('power loss')
        with self.assertRaises(OSError):copy_recording(self.base,self.source,self.guard,interrupt,transaction_id=transaction)
        partial=self.usb/('.headring-export-'+transaction+'.partial')
        self.assertTrue(partial.is_dir())
        copy_recording(self.base,self.source,self.guard,transaction_id=transaction)
        self.assertFalse(partial.exists());self.assertTrue(self.source.exists())

    @patch('usb_export_copy.os.sync')
    def test_unexpected_partial_content_is_never_deleted(self, _):
        transaction='c'*32;partial=self.usb/('.headring-export-'+transaction+'.partial');partial.mkdir()
        (partial/'foreign').write_bytes(b'keep')
        with self.assertRaises(CloudError):copy_recording(self.base,self.source,self.guard,transaction_id=transaction)
        self.assertEqual((partial/'foreign').read_bytes(),b'keep')

    @patch('usb_export_copy.os.sync')
    def test_restart_after_rename_revalidates_before_receipt(self, _):
        transaction='a'*32
        with patch('usb_export_copy.atomic',side_effect=OSError('power loss')):
            with self.assertRaises(OSError):
                copy_recording(self.base,self.source,self.guard,transaction_id=transaction)
        destination=self.usb/('recording-'+transaction)
        self.assertTrue(destination.is_dir())
        path,receipt=copy_recording(self.base,self.source,self.guard,transaction_id=transaction)
        self.assertTrue(path.is_file());self.assertEqual(receipt['destination'],destination.name)
        (destination/'stream'/'data').write_bytes(b'bad')
        with self.assertRaises(CloudError):
            copy_recording(self.base,self.source,self.guard,transaction_id=transaction)
        self.assertTrue(self.source.exists())

    @patch('usb_export_copy.os.sync')
    def test_existing_destination_never_overwritten(self, _):
        (self.usb/'recording').mkdir()
        with self.assertRaises(CloudError):
            copy_recording(self.base, self.source, self.guard)
        self.assertTrue(self.source.exists())

    @patch('usb_export_copy.os.sync')
    def test_insufficient_space_keeps_source(self, _):
        with patch('usb_export_copy.os.fstatvfs', return_value=type('Usage', (), {'f_bavail': 0, 'f_frsize': 1})()):
            with self.assertRaises(CloudError):
                copy_recording(self.base, self.source, self.guard)
        self.assertTrue(self.source.exists())
        self.assertEqual(list(self.usb.iterdir()), [])

    @patch('usb_export_copy.os.sync')
    def test_disconnect_during_write_never_commits(self, _):
        def progress(phase, done, total):
            if phase == 'copying': raise CloudError('拔出U盘')
        with self.assertRaises(CloudError):
            copy_recording(self.base, self.source, self.guard, progress)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.usb/'recording').exists())
        self.assertFalse((self.base/'runtime/usb_exports/receipts').exists())

    @patch('usb_export_copy.os.sync')
    def test_disconnect_during_copy_keeps_source(self, _):
        calls = [0]; original = self.guard.check
        def check():
            calls[0] += 1
            if calls[0] == 7: raise CloudError('U盘已断开')
            return original()
        with patch.object(self.guard, 'check', side_effect=check):
            with self.assertRaises(CloudError):
                copy_recording(self.base, self.source, self.guard)
        self.assertTrue((self.source/'stream'/'data').exists())
        self.assertFalse((self.usb/'recording').exists())

if __name__ == '__main__':
    unittest.main()
