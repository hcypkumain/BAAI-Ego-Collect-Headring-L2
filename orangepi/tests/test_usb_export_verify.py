import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control'))
from cloud_common import CloudError
from usb_export_verify import verify_export


class ExportVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'file').write_bytes(b'abc')
        self.manifest = {'files': [{'path': 'file', 'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}]}

    def test_success(self):
        self.assertEqual(verify_export(self.root, self.manifest)['bytes'], 3)

    def test_same_size_corruption_is_rejected(self):
        (self.root / 'file').write_bytes(b'xyz')
        with self.assertRaises(CloudError):
            verify_export(self.root, self.manifest)

    def test_missing_and_extra_files_are_rejected(self):
        (self.root / 'file').unlink()
        with self.assertRaises(CloudError):
            verify_export(self.root, self.manifest)
        (self.root / 'file').write_bytes(b'abc')
        (self.root / 'extra').touch()
        with self.assertRaises(CloudError):
            verify_export(self.root, self.manifest)

    def test_symlink_is_rejected(self):
        (self.root / 'file').unlink()
        (self.root / 'file').symlink_to('/dev/null')
        with self.assertRaises((CloudError, OSError)):
            verify_export(self.root, self.manifest)

if __name__ == '__main__':
    unittest.main()
