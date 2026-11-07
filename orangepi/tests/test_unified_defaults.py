"""Fresh-install defaults never override an existing station's active profile."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import collection_profiles

class DefaultsTests(unittest.TestCase):
    def test_missing_default_uses_emg(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(collection_profiles.selection(root)['active_mode'],'emg')

    def test_existing_eeg_mode_wins_over_repository_emg_default(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'runtime').mkdir();(root/'configs').mkdir()
            old=dict(version=1,active_mode='eeg',legacy_mode='eeg',generation=7)
            (root/'runtime/collection_profile.json').write_text(json.dumps(old))
            (root/'configs/collection_mode.json').write_text('{"default_mode":"emg"}')
            self.assertEqual(collection_profiles.selection(root),old)

if __name__=='__main__':unittest.main()
