import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src/orangepi_collect/common'), str(ROOT/'src/orangepi_collect/control')]
from usb_export_gate import pending_exports


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)/'orangepi'; (self.base/'configs').mkdir(parents=True)
        (self.base/'configs/collection_mode.json').write_text('{"default_mode":"emg"}')
        self.other = self.base.parent/'profiles/eeg/orangepi/runtime/usb_exports/pending.json'
        self.other.parent.mkdir(parents=True)

    def test_no_transaction_does_not_block(self):
        self.assertEqual(pending_exports(self.base), [])

    def test_other_profile_pending_blocks(self):
        self.other.write_text(json.dumps({'version': 1, 'state': 'purging'}))
        self.assertEqual(pending_exports(self.base)[0]['state'], 'purging')

    def test_completed_does_not_block(self):
        self.other.write_text(json.dumps({'version': 1, 'state': 'complete'}))
        self.assertEqual(pending_exports(self.base), [])

    def test_corrupt_or_symlinked_state_blocks(self):
        self.other.write_text('{')
        self.assertTrue(pending_exports(self.base))
        self.other.unlink(); self.other.symlink_to('/nonexistent')
        self.assertTrue(pending_exports(self.base))

class ControllerGateTests(unittest.TestCase):
    def test_export_states_offer_no_capture_actions(self):
        from dashboard import actions_for
        for state in ('usb_exporting', 'usb_export_recovery'):
            actions = actions_for(state)
            self.assertFalse(set(actions) & {'prepare', 'start', 'collection_mode', 'usb_task_import'})

class ControllerRecoveryTests(unittest.TestCase):
    def test_completed_startup_recovery_returns_to_device_confirm(self):
        from reference_controls import ReferenceControlsMixin
        controller=SimpleNamespace(state='usb_export_recovery',usb_export_service=SimpleNamespace(active=lambda:False),
                                   dashboard=None,state_write=Mock())
        with patch('usb_export_gate.pending_exports',return_value=[]):
            ReferenceControlsMixin.poll_usb_export(controller)
        self.assertEqual(controller.state,'device_confirm')
        controller.state_write.assert_called_once()

class ControllerActionTests(unittest.TestCase):
    def test_start_enters_exclusive_state_and_restores_after_completion(self):
        from reference_controls import ReferenceControlsMixin
        cloud=SimpleNamespace(lock=threading.RLock(),active=None,process=None,state={'paused':False},_save=Mock())
        service=SimpleNamespace(start=Mock(),active=lambda:False)
        dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':False,'stop_confirmed':True,'phase':'idle'}),cloud_uploads=cloud)
        controller=SimpleNamespace(state='idle',dashboard=dashboard,usb_export_service=service,
            usb_task_importer=SimpleNamespace(active=False),reference_can_change=lambda:True,state_write=Mock())
        with patch('usb_export_gate.pending_exports',return_value=[]):
            ReferenceControlsMixin.start_usb_export(controller)
            self.assertEqual(controller.state,'usb_exporting');self.assertTrue(cloud.state['paused'])
            service.start.assert_called_once_with(recover=False)
            ReferenceControlsMixin.poll_usb_export(controller)
        self.assertEqual(controller.state,'idle');self.assertFalse(cloud.state['paused'])

    def test_active_import_blocks_start(self):
        from reference_controls import ReferenceControlsMixin
        dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':False,'stop_confirmed':True,'phase':'idle'}))
        controller=SimpleNamespace(state='idle',dashboard=dashboard,usb_export_service=SimpleNamespace(start=Mock()),
            usb_task_importer=SimpleNamespace(active=True),reference_can_change=lambda:True)
        with patch('usb_export_gate.pending_exports',return_value=[]):
            with self.assertRaises(ValueError):ReferenceControlsMixin.start_usb_export(controller)
        self.assertEqual(controller.state,'idle')
