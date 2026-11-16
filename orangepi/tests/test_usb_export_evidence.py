import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control'))
from usb_export_evidence import evidence_path, successful_export


class ExportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.base = self.root/'base'; self.project = self.root/'project'
        self.image = self.project/'configs/environments/test.png'; self.image.parent.mkdir(parents=True); self.image.touch()
        self.source = str(self.root/'sd/data/session-1'); self.entry = {'session': self.source, 'status': 'success'}
        rows = [{'path': 'video', 'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.record = {'version': 1, 'state': 'complete', 'source': self.source, 'reference_path': 'configs/environments/test.png', 'qualified_before_export': True, 'local_deleted': True, 'verification': 'sha256', 'manifest': {'files': rows, 'digest': digest}}
        self.path = evidence_path(self.base, self.source); self.path.parent.mkdir(parents=True); self.save()

    def save(self): self.path.write_text(json.dumps(self.record))
    def valid(self): return successful_export(self.base, self.project, self.entry, self.image)
    def test_bound_qualified_receipt(self): self.assertTrue(self.valid())
    def test_incomplete_and_pending_are_not_success(self):
        for key, value in [('state','prepared'),('qualified_before_export',False),('local_deleted',False),('verification','size')]:
            old=self.record[key];self.record[key]=value;self.save();self.assertFalse(self.valid());self.record[key]=old
    def test_other_reference_or_modified_manifest_rejected(self):
        self.record['reference_path']='configs/environments/other.png';self.save();self.assertFalse(self.valid())
        self.record['reference_path']='configs/environments/test.png';self.record['manifest']['files'][0]['bytes']=4;self.save();self.assertFalse(self.valid())
    def test_source_still_exists_not_tombstone(self):
        Path(self.source).mkdir(parents=True);self.assertFalse(self.valid())

if __name__ == '__main__': unittest.main()
