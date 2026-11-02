"""Exercise the real native Capture gate without ROS or acquisition devices."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]/'src/orangepi_collect/acquisition/capture.py'


def native_capture():
    """Load unchanged runtime methods with inert import-time dependencies."""
    frozen = {'emg': {}, 'cameras': {}, 'wrist_cameras': {},
              'board_imu': {'topic': '/frozen/headband/imu'},
              'headband_calibration': {'manifest': 'frozen/candidate.json'}}
    paths = types.ModuleType('project_paths')
    paths.BASE = SOURCE.parents[3]; paths.PROJECT = paths.BASE.parent
    paths.SRC = SOURCE.parents[1]; paths.devices = Mock(return_value=frozen)
    gst = types.SimpleNamespace(init=Mock())
    gi = types.ModuleType('gi'); gi.require_version = Mock()
    repository = types.ModuleType('gi.repository'); repository.Gst = gst
    ros = types.ModuleType('rospy'); ros.init_node = Mock(); ros.signal_shutdown = Mock()
    sensor = types.ModuleType('sensor_msgs'); messages = types.ModuleType('sensor_msgs.msg')
    messages.CompressedImage = object
    preview = types.ModuleType('live_preview'); preview.publisher = Mock()
    camera = types.ModuleType('orangepi_collect.recorders.camera.mpp_camera_recorder')
    camera.Camera = Mock()
    hbc = types.ModuleType('headband_calibration')
    hbc.snapshot = Mock(); hbc.verify_saved = Mock()
    imports = {'project_paths': paths, 'gi': gi, 'gi.repository': repository,
               'rospy': ros, 'sensor_msgs': sensor, 'sensor_msgs.msg': messages,
               'live_preview': preview, 'headband_calibration': hbc,
               'orangepi_collect.recorders.camera.mpp_camera_recorder': camera,
               'numpy': types.ModuleType('numpy')}
    spec = importlib.util.spec_from_file_location('_headband_native_capture_test', str(SOURCE))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, imports), patch.object(sys, 'path', list(sys.path)):
        spec.loader.exec_module(module)
    # A later helper must use the import-time frozen CONFIG, never rediscover.
    paths.devices.side_effect = AssertionError('Must use the frozen CONFIG')
    return module, frozen


def candidate(state='candidate'):
    return {'state': state, 'available': state == 'candidate',
            'applicability_verified': False, 'conflict': False,
            'message': 'Existing candidate has not been physically verified',
            'snapshot': 'headband_calibration/manifest.json'}


class HeadbandNativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.native, self.frozen = native_capture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.native.Capture(self.root)

    def read(self, name):
        return json.loads((self.root/name).read_text())

    def execute_without_devices(self):
        self.capture.commands.put('START'); self.capture.commands.put('STOP')
        with patch.object(self.native.Gst, 'init') as gst, \
             patch.object(self.native.rospy, 'init_node') as init, \
             patch.object(self.native.rospy, 'signal_shutdown') as shutdown, \
             patch.object(self.native.signal, 'signal'), \
             patch.object(threading.Thread, 'start'), \
             patch.object(self.native, 'EMG', side_effect=AssertionError('No EMG device')), \
             patch.object(self.native, 'BoardImu', side_effect=AssertionError('No IMU device')), \
             patch.object(self.native, 'Camera', side_effect=AssertionError('No camera device')):
            result = self.capture.execute([], with_emg=False, with_board=False,
                                          emg_names=[], wrist_names=())
        gst.assert_called_once_with(None)
        init.assert_called_once_with('collect_ros_emg', anonymous=True, disable_signals=True)
        shutdown.assert_called_once_with('capture complete')
        return result

    def test_head_camera_snapshot_receives_exact_import_time_frozen_config(self):
        status = candidate(); self.native.headband_calibration.snapshot.return_value = status
        self.capture.prepare_headband_calibration(['left'], False)
        self.native.headband_calibration.snapshot.assert_called_once_with(self.root, self.frozen)
        self.assertIs(self.native.headband_calibration.snapshot.call_args[0][1], self.native.CONFIG)
        self.assertIs(self.capture.headband_calibration_status, status)
        self.native.devices.assert_called_once_with()

    def test_head_imu_alone_also_snapshots_without_camera(self):
        self.native.headband_calibration.snapshot.return_value = candidate()
        self.capture.prepare_headband_calibration([], True)
        self.native.headband_calibration.snapshot.assert_called_once_with(self.root, self.frozen)

    def test_wrist_emg_or_no_headband_does_not_snapshot(self):
        self.capture.prepare_headband_calibration([], False)
        self.assertIsNone(self.capture.headband_calibration_status)
        self.native.headband_calibration.snapshot.assert_not_called()
        self.capture.verify_headband_calibration()
        self.native.headband_calibration.verify_saved.assert_not_called()
        self.assertEqual(self.execute_without_devices(), 0)
        self.assertTrue(self.capture.started)
        self.assertIsNone(self.read('capture_summary.json')['headband_calibration'])
        self.native.headband_calibration.snapshot.assert_not_called()
        self.native.headband_calibration.verify_saved.assert_not_called()

    def test_unverified_candidate_or_missing_file_status_can_start_when_saved_check_passes(self):
        for state in ('candidate', 'not_available'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory); self.capture = self.native.Capture(self.root)
                status = candidate(state); self.capture.headband_calibration_status = status
                self.native.headband_calibration.verify_saved.reset_mock()
                self.assertEqual(self.execute_without_devices(), 0)
                self.native.headband_calibration.verify_saved.assert_called_once_with(self.root, status)
                self.assertTrue(self.capture.started)
                self.assertFalse(self.capture.active)
                self.assertGreater(self.capture.start_ns, 0)
                gate = self.read('start_gate.json')
                self.assertEqual(gate['start_monotonic_ns'], self.capture.start_ns)
                for filename in ('status.json', 'capture_summary.json'):
                    saved = self.read(filename)
                    self.assertEqual(saved['headband_calibration'], status)
                    self.assertEqual(saved['status' if filename == 'capture_summary.json' else 'state'], 'success')
                self.assertEqual(self.read('capture_summary.json')['warnings'], [])
                self.assertEqual(self.read('status.json')['warning_count'], 0)

    def test_changed_saved_snapshot_rejects_start_before_gate_or_started_side_effect(self):
        status = candidate(); self.capture.headband_calibration_status = status
        self.native.headband_calibration.verify_saved.side_effect = RuntimeError('saved headband snapshot changed')
        self.assertEqual(self.execute_without_devices(), 1)
        self.native.headband_calibration.verify_saved.assert_called_once_with(self.root, status)
        self.assertFalse(self.capture.started)
        self.assertFalse(self.capture.active)
        self.assertEqual(self.capture.start_ns, 0)
        self.assertEqual(self.capture.start_wall_ns, 0)
        self.assertFalse((self.root/'start_gate.json').exists())
        self.assertTrue((self.root/'stop_gate.json').exists())
        summary = self.read('capture_summary.json')
        self.assertEqual(summary['status'], 'error')
        self.assertIn('saved headband snapshot changed', summary['errors'])
        self.assertEqual(summary['headband_calibration'], status)
        self.assertEqual(self.read('status.json')['headband_calibration'], status)
        self.assertEqual(self.read('status.json')['warning_count'], 0)

    def test_snapshot_conflict_rejects_before_opening_any_selected_device(self):
        self.capture.commands.put('START')
        self.native.headband_calibration.snapshot.side_effect = RuntimeError('headband snapshot conflict')
        with patch.object(self.native.signal, 'signal'), patch.object(threading.Thread, 'start'), \
             patch.object(self.native, 'EMG') as emg, patch.object(self.native, 'BoardImu') as board, \
             patch.object(self.native, 'Camera') as camera:
            result = self.capture.execute(['left'], with_emg=True, with_board=True,
                                          emg_names=[], wrist_names=())
        self.assertEqual(result, 1)
        emg.assert_not_called(); board.assert_not_called(); camera.assert_not_called()
        self.assertFalse(self.capture.started)
        self.assertFalse((self.root/'start_gate.json').exists())
        self.native.headband_calibration.verify_saved.assert_not_called()
        self.assertIn('headband snapshot conflict', self.read('capture_summary.json')['errors'])

    def test_returned_snapshot_conflict_status_also_rejects_before_devices_open(self):
        conflict = candidate('not_available'); conflict['conflict'] = True
        self.native.headband_calibration.snapshot.return_value = conflict
        self.capture.commands.put('START')
        with patch.object(self.native.signal, 'signal'), patch.object(threading.Thread, 'start'), \
             patch.object(self.native, 'BoardImu') as board, patch.object(self.native, 'Camera') as camera:
            result = self.capture.execute(['left'], with_emg=False, with_board=True,
                                          emg_names=[], wrist_names=())
        self.assertEqual(result, 1)
        board.assert_not_called(); camera.assert_not_called()
        self.assertFalse(self.capture.started)
        self.assertFalse((self.root/'start_gate.json').exists())
        summary = self.read('capture_summary.json')
        self.assertEqual(summary['headband_calibration'], conflict)
        self.assertTrue(summary['errors'])
        self.native.headband_calibration.verify_saved.assert_not_called()

    def test_calibration_advisory_is_separate_from_data_continuity_warnings(self):
        status = candidate(); self.capture.headband_calibration_status = status
        self.capture.warning_list.append('emg_left: serial sequence gap=1')
        self.capture.status('ready')
        saved = self.read('status.json')
        self.assertEqual(saved['headband_calibration'], status)
        self.assertEqual(saved['warning_count'], 1)
        self.assertEqual(saved['last_warning'], 'emg_left: serial sequence gap=1')
        self.assertEqual(self.execute_without_devices(), 0)
        summary = self.read('capture_summary.json')
        self.assertEqual(summary['warnings'], ['emg_left: serial sequence gap=1'])
        self.assertEqual(summary['headband_calibration'], status)
        self.assertEqual(self.read('status.json')['warning_count'], 1)
        self.assertFalse((self.root/'health_events.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
