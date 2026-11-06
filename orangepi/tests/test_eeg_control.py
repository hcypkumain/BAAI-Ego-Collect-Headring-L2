"""Python 3.8: identity, gate boundaries and device ownership."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import device_discovery
from eeg_settings import DEFAULT, apply
from eeg_worker import HostGate
from eeg_bridge import EegProcess
import dashboard


class EegControlTests(unittest.TestCase):
    def test_exact_serial_binding_rejects_other_esp32(self):
        cfg = {'eeg':copy.deepcopy(DEFAULT)}
        unknown = dict(kind='eeg', serial='OTHER', path='/dev/ttyACM0')
        result,_ = device_discovery.resolve(cfg,[unknown],{})
        self.assertFalse(result['eeg']['discovery']['present'])
        known = dict(unknown, serial=DEFAULT['serial'], path='/dev/serial/by-id/known')
        result,_ = device_discovery.resolve(cfg,[unknown,known],{})
        self.assertEqual(result['eeg']['port'], known['path'])
        result,_ = device_discovery.resolve(cfg,[known,dict(known,path='/dev/duplicate')],{})
        self.assertFalse(result['eeg']['discovery']['present'])

    def test_filter_choice_persists_without_overwriting_device_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'runtime').mkdir()
            (root/'runtime/eeg_preferences.json').write_text('{"filter_preset":"raw"}')
            cfg=apply({'eeg':{'serial':'known'},'emg':{'left':1}},root)
            self.assertEqual(cfg['eeg']['serial'],'known')
            self.assertEqual(cfg['eeg']['filter_preset'],'raw')
            self.assertEqual(cfg['emg'],{'left':1})
            (root/'runtime/eeg_preferences.json').write_text('{"filter_preset":"unknown"}')
            with self.assertRaises(ValueError):apply({},root)

    def test_gate_excludes_preparation_crossing_start_and_after_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);gate=HostGate(root)
            self.assertFalse(gate.accepts(10,20,20))
            (root/'start_gate.json').write_text('{"start_monotonic_ns":100}')
            self.assertFalse(gate.accepts(99,110,110))
            self.assertFalse(gate.accepts(100,110,99))
            self.assertTrue(gate.accepts(100,110,110))
            (root/'stop_gate.json').write_text('{"stop_monotonic_ns":120}')
            self.assertTrue(gate.accepts(110,119,119))
            self.assertFalse(gate.accepts(110,120,120))
            self.assertFalse(gate.accepts(120,130,130))
            # Queued pre-STOP data may still be saved after STOP is published.
            self.assertTrue(gate.accepts(110,119,119))

    def test_configuration_actions_locked_in_recording(self):
        self.assertIn('eeg_filter',dashboard.actions_for('ready'))
        self.assertIn('stop',dashboard.actions_for('recording'))
        self.assertNotIn('eeg_filter',dashboard.actions_for('recording'))
        self.assertNotIn('eeg_filter',dashboard.actions_for('confirm_stop'))

    def test_abnormal_child_exit_is_fatal_and_cannot_report_ready(self):
        bridge=EegProcess.__new__(EegProcess)
        bridge.process=Mock();bridge.process.poll.return_value=1
        bridge.process.returncode=1;bridge.error='disconnected'
        with self.assertRaisesRegex(RuntimeError,'disconnected'):bridge.health()


if __name__=='__main__':unittest.main()
