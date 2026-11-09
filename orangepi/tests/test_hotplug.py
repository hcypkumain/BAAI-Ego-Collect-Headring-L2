import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import project_paths
import launcher
import scan_devices
from scan_result import encode


class HotplugTests(unittest.TestCase):
    def controller(self):
        c = launcher.Controller.__new__(launcher.Controller)
        c.worker = None
        c.keyboard = None
        c.state = 'device_confirm'
        c.last_usb_poll = 0
        c.last_device_scan = 0
        c.last_scan = []
        c.usb_fingerprint = ('before',)
        c.scan_devices = Mock()
        return c

    def test_unplug_changes_inventory_and_automatically_rescans(self):
        c = self.controller()
        with patch.object(launcher, 'devices', return_value={'auto_discovery': {'enabled': True}}), \
             patch.object(launcher, 'fingerprint', return_value=('after',)):
            c.poll_hotplug()
        c.scan_devices.assert_called_once_with(announce=False)

    def test_no_background_reader_while_capture_or_finalization_owns_worker(self):
        c = self.controller(); c.worker = Mock()
        with patch.object(launcher, 'devices', side_effect=AssertionError('must not rescan')):
            for state in ('preparing', 'recording', 'finalizing', 'failed_wait'):
                c.state = state; c.poll_hotplug()
        c.scan_devices.assert_not_called()

    def test_present_but_silent_receiver_is_retried_without_replug(self):
        c = self.controller(); c.last_scan = [{'present': True, 'connected': False}]
        with patch.object(launcher, 'devices', return_value={'auto_discovery': {'enabled': True}}), \
             patch.object(launcher, 'fingerprint', return_value=c.usb_fingerprint):
            c.poll_hotplug()
        c.scan_devices.assert_called_once_with(announce=False)

    def test_unchanged_ready_inventory_does_not_reopen_devices(self):
        c = self.controller(); c.last_scan = [{'present': True, 'connected': True}]
        with patch.object(launcher, 'devices', return_value={'auto_discovery': {'enabled': True}}), \
             patch.object(launcher, 'fingerprint', return_value=c.usb_fingerprint):
            c.poll_hotplug()
        c.scan_devices.assert_not_called()

    def test_recording_uses_frozen_config_even_after_usb_assignment_changes(self):
        frozen = {'emg': {'emg_left': {'port': '/dev/original'}}, 'wrist_cameras': {}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'selection.json'
            path.write_text(json.dumps({'selected': ['emg_left'], 'device_config': frozen}))
            with patch.dict(os.environ, {'ORANGEPI_DEVICE_SELECTION': str(path)}), \
                 patch('device_discovery.discover', side_effect=AssertionError('must stay frozen')):
                self.assertEqual(project_paths.devices(), frozen)

    def test_event_during_scan_is_seen_again_on_next_poll(self):
        c = self.controller()
        c.state_write = Mock(); c.voice = Mock(); c.stop_preview=Mock()
        c.scan_lock=threading.Lock();c.camera_registry=object()
        rows = [{'name': 'cam_left_wrist', 'label': '相机 A', 'connected': True,
                 'present': True, 'device': '/dev/current','status':'ready'}]
        result = Mock(returncode=0, stdout=json.dumps(rows))
        def fake_scan(*a,**kw):
            req=json.loads(kw['env']['ORANGEPI_SCAN_REQUEST'])
            return encode('run','nonce',req['id'],req['fingerprint'],rows),'run','nonce'
        with patch.object(launcher, 'devices', return_value={'auto_discovery': {'enabled': True}}), \
             patch.object(launcher, 'fingerprint', side_effect=[(('wrist','/dev/current','/dev/video1','x',1),), (('wrist','/dev/current','/dev/video2','x',2),), (('wrist','/dev/current','/dev/video2','x',2),)]), \
             patch.object(launcher, 'run_scan', side_effect=fake_scan), \
             patch.object(launcher, 'atomic_json'):
            self.assertFalse(launcher.Controller.scan_devices(c, announce=False,quiet=True))
            self.assertFalse(c.scan_ok)
            c.last_usb_poll=0;c.poll_hotplug()
        c.scan_devices.assert_called_once_with(announce=False)

    def test_missing_and_no_data_are_distinct(self):
        item = {'label': '肌电 A', 'port': '/dev/receiver', 'discovery': {'present': False}}
        missing = scan_devices.check('emg', 'emg_left', item)
        self.assertEqual(missing['status'], 'missing')
        item['discovery']['present'] = True
        with patch.object(scan_devices.Path, 'exists', return_value=True), \
             patch.object(scan_devices, 'serial_probe', return_value={'ready': False, 'bytes_read': 0}):
            silent = scan_devices.check('emg', 'emg_left', item)
        self.assertTrue(silent['present'])
        self.assertFalse(silent['connected'])
        self.assertEqual(silent['status'], 'no_data')
        self.assertIn('未输出数据', silent['error'])

    def test_serial_data_with_wrong_protocol_is_not_called_ready(self):
        item = {'label': '肌电 A', 'port': '/dev/receiver'}
        with patch.object(scan_devices.Path, 'exists', return_value=True), \
             patch.object(scan_devices, 'serial_probe', return_value={'ready': False, 'bytes_read': 128}):
            result = scan_devices.check('emg', 'emg_left', item)
        self.assertFalse(result['connected'])
        self.assertIn('协议', result['error'])


if __name__ == '__main__':
    unittest.main()
