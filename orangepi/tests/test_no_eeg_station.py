import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard,live_preview,display_demand

class NoEegTests(unittest.TestCase):
    def test_actions_remain_recordable_even_with_legacy_environment(self):
        with patch.dict(os.environ,{'OMNIBCI_ENABLE_IMPEDANCE_PREFLIGHT':'1'}):
            actions=dashboard.actions_for('ready')
        self.assertIn('start',actions);self.assertNotIn('eeg_filter',actions)
        self.assertFalse(any('impedance' in action for action in actions))
    def test_preview_and_demand_only_include_emg(self):
        self.assertEqual(set(live_preview.SIGNALS),{'emg_left','emg_right'})
        self.assertEqual(set(display_demand.SIGNALS),{'emg_left','emg_right'})
        self.assertFalse(hasattr(live_preview.Publisher,'eeg'))
    def test_stale_snapshot_cannot_restore_retired_modality(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'devices.json';p.write_text(json.dumps(dict(device_config=dict(eeg=dict(enabled=True),emg={}))))
            with patch.dict(os.environ,{'ORANGEPI_DEVICE_SELECTION':str(p)}):self.assertNotIn('eeg',project_paths.devices())
    def test_pages_have_no_brain_or_impedance_controls(self):
        root=Path(dashboard.__file__).parent
        for name in ('dashboard.html','phone_shell.js'):
            source=(root/name).read_text(encoding='utf-8')
            self.assertNotIn('脑电',source);self.assertNotIn('impedance',source)

if __name__=='__main__':unittest.main()
