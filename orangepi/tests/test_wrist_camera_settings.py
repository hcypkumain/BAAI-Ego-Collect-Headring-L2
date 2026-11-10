"""Policy, readback and transaction tests; no Pi or camera driver is needed."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from orangepi_collect.common import wrist_camera_settings as settings
from orangepi_collect.recorders.camera.uvc_source import UvcSource


class Controls:
    def __init__(self, values, unsupported=(), ignore_set=(), failed_readback=(), failed_rollback=()):
        self.values = dict(values)
        self.unsupported = set(unsupported)
        self.ignore_set = set(ignore_set)
        self.failed_readback = set(failed_readback)
        self.failed_rollback = set(failed_rollback)
        self.calls = []
        self.set_count = {}

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.assert_contract(args, kwargs)
        device, option = args[2], args[3]
        if option.startswith('--get-ctrl='):
            if device in self.unsupported:
                return SimpleNamespace(returncode=1, stdout='', stderr="unknown control 'exposure_auto_priority'")
            if device in self.failed_readback and self.set_count.get(device, 0):
                return SimpleNamespace(returncode=0, stdout='exposure_auto_priority: unknown\n', stderr='')
            return SimpleNamespace(returncode=0, stdout='exposure_auto_priority: %d\n' % self.values[device], stderr='')
        self.set_count[device] = self.set_count.get(device, 0) + 1
        value = bool(int(option.rsplit('=', 1)[1]))
        if device in self.failed_rollback and self.set_count[device] > 1:
            return SimpleNamespace(returncode=1, stdout='', stderr='rollback device busy')
        if device not in self.ignore_set:
            self.values[device] = value
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def assert_contract(self, args, kwargs):
        assert args[:2] == ['v4l2-ctl', '--device']
        assert args[3] == '--get-ctrl=exposure_auto_priority' or args[3] in (
            '--set-ctrl=exposure_auto_priority=0', '--set-ctrl=exposure_auto_priority=1')
        assert kwargs['timeout'] == 4
        assert kwargs['universal_newlines'] is True

    @property
    def writes(self):
        return [args for args in self.calls if args[3].startswith('--set-ctrl=')]


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_and_all_three_values_round_trip(self):
        self.assertEqual(settings.load(self.base), {settings.KEY: None})
        for value in (False, True, None):
            settings.save(self.base, value)
            self.assertEqual(settings.load(self.base), {settings.KEY: value})
        self.assertFalse(list((self.base / 'runtime').glob('*.tmp')))

    def test_strict_type_rejects_integers_and_strings_without_replacing_file(self):
        settings.save(self.base, False)
        path = self.base / 'runtime' / settings.PREFERENCES_FILE
        original = path.read_bytes()
        for value in (0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.save(self.base, value)
            self.assertEqual(path.read_bytes(), original)

    def test_corrupt_preferences_are_errors_and_are_not_silently_reset(self):
        path = self.base / 'runtime' / settings.PREFERENCES_FILE
        path.parent.mkdir()
        for text in ('{', '[]', '{}', '{"%s": 1}' % settings.KEY, '{"%s": "false"}' % settings.KEY):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Invalid wrist camera preferences'):
                settings.load(self.base)
            self.assertEqual(path.read_text(), text)

    def test_apply_deepcopies_and_overwrites_a_stale_snapshot_even_when_none(self):
        config = {'wrist_camera_settings': {settings.KEY: True}, 'wrist_cameras': {
            'left': {'device': '/dev/left', settings.KEY: True, 'nested': {'value': 1}},
            'right': {'device': '/dev/right', settings.KEY: False}}}
        original = copy.deepcopy(config)
        applied = settings.apply(config, self.base)
        self.assertEqual(applied['wrist_camera_settings'], {settings.KEY: None})
        self.assertIsNone(applied['wrist_cameras']['left'][settings.KEY])
        applied['wrist_cameras']['left']['nested']['value'] = 2
        self.assertEqual(config, original)
        settings.save(self.base, False)
        self.assertIs(settings.apply(config, self.base)['wrist_cameras']['right'][settings.KEY], False)

    def test_nonblocking_locks_and_capture_probe_preserve_file_contents(self):
        path = self.base / 'runtime' / 'capture.lock'
        path.parent.mkdir()
        path.write_text('existing marker')
        self.assertTrue(settings.capture_available(self.base))
        with settings.capture_guard(self.base):
            self.assertFalse(settings.capture_available(self.base))
            with self.assertRaisesRegex(ValueError, 'capture is active'):
                with settings.capture_guard(self.base):
                    self.fail('second lock acquired')
            with settings.preference_lock(self.base):
                with self.assertRaisesRegex(ValueError, 'being changed'):
                    with settings.preference_lock(self.base):
                        self.fail('second preference lock acquired')
        self.assertTrue(settings.capture_available(self.base))
        self.assertEqual(path.read_text(), 'existing marker')

    def test_atomic_writer_failure_keeps_old_preferences(self):
        settings.save(self.base, True)
        with patch.object(settings.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                settings.save(self.base, False)
        self.assertEqual(settings.load(self.base), {settings.KEY: True})
        self.assertFalse(list((self.base / 'runtime').glob('*.tmp')))

    def test_directory_sync_failure_after_replace_does_not_claim_save_failed(self):
        settings.save(self.base, True)
        original_fsync = settings.os.fsync
        calls = []
        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('directory fsync unsupported')
            return original_fsync(fd)
        with patch.object(settings.os, 'fsync', side_effect=fsync):
            settings.save(self.base, False)
        self.assertEqual(settings.load(self.base), {settings.KEY: False})


class DeviceTests(unittest.TestCase):
    def test_default_none_only_queries_and_reports_actual_value(self):
        fake = Controls({'/dev/left': True})
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            report = settings.configure_device({'device': '/dev/left'})
        self.assertIsNone(report['requested'])
        self.assertIs(report['actual_allow_auto_exposure_fps_change'], True)
        self.assertIs(report['supported'], True)
        self.assertFalse(report['applied'])
        self.assertEqual(fake.writes, [])

    def test_both_boolean_choices_are_set_then_read_back(self):
        for requested in (False, True):
            fake = Controls({'/dev/left': not requested})
            with self.subTest(requested=requested), patch.object(settings.subprocess, 'run', side_effect=fake):
                report = settings.configure_device({'device': '/dev/left', settings.KEY: requested})
            self.assertIs(report['actual_allow_auto_exposure_fps_change'], requested)
            self.assertTrue(report['applied'])
            self.assertEqual(len(fake.writes), 1)
            self.assertTrue(fake.calls[-1][3].startswith('--get-ctrl='))

    def test_unsupported_preserve_is_reported_but_explicit_requests_fail(self):
        for requested in (None, False, True):
            fake = Controls({}, unsupported=['/dev/left'])
            with self.subTest(requested=requested), patch.object(settings.subprocess, 'run', side_effect=fake):
                if requested is None:
                    report = settings.configure_device({'device': '/dev/left', settings.KEY: requested})
                else:
                    with self.assertRaises(settings.WristCameraSettingsError) as error:
                        settings.configure_device({'device': '/dev/left', settings.KEY: requested})
                    report = error.exception.report
            self.assertIs(report['supported'], False)
            self.assertFalse(report['applied'])
            self.assertIsNone(report['actual_allow_auto_exposure_fps_change'])
            self.assertEqual(fake.writes, [])

    def test_readback_mismatch_never_claims_applied(self):
        fake = Controls({'/dev/left': False}, ignore_set=['/dev/left'])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            with self.assertRaisesRegex(settings.WristCameraSettingsError, 'readback mismatch') as error:
                settings.configure_device({'device': '/dev/left', settings.KEY: True})
        self.assertFalse(error.exception.report['applied'])
        self.assertIs(error.exception.report['actual_allow_auto_exposure_fps_change'], False)

    def test_failed_query_and_failed_readback_are_not_supported_or_actual_guesses(self):
        with patch.object(settings.subprocess, 'run', side_effect=FileNotFoundError('v4l2-ctl missing')):
            report = settings.configure_device({'device': '/dev/left'})
            self.assertIsNone(report['supported'])
            self.assertIsNone(report['actual_allow_auto_exposure_fps_change'])
            with self.assertRaises(settings.WristCameraSettingsError):
                settings.configure_device({'device': '/dev/left', settings.KEY: False})
        fake = Controls({'/dev/left': False}, failed_readback=['/dev/left'])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            with self.assertRaisesRegex(settings.WristCameraSettingsError, 'Invalid .* readback') as error:
                settings.configure_device({'device': '/dev/left', settings.KEY: True})
        self.assertFalse(error.exception.report['applied'])
        self.assertIsNone(error.exception.report['actual_allow_auto_exposure_fps_change'])

    def test_nonboolean_device_requests_are_rejected_before_any_command(self):
        with patch.object(settings.subprocess, 'run') as run:
            for value in (0, 1, 'false'):
                with self.assertRaises(ValueError):
                    settings.configure_device({'device': '/dev/left', settings.KEY: value})
        run.assert_not_called()

    def test_set_timeout_still_queries_and_never_claims_verified_application(self):
        fake = Controls({'/dev/left': False})
        def run(args, **kwargs):
            if args[3].startswith('--set-ctrl='):
                fake.values['/dev/left'] = True
                raise subprocess.TimeoutExpired(args, 4)
            return fake(args, **kwargs)
        with patch.object(settings.subprocess, 'run', side_effect=run):
            with self.assertRaises(settings.WristCameraSettingsError) as error:
                settings.configure_device({'device': '/dev/left', settings.KEY: True})
        self.assertFalse(error.exception.report['applied'])
        self.assertIs(error.exception.report['actual_allow_auto_exposure_fps_change'], True)
        self.assertEqual(len(fake.calls), 2)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.devices = {name: str(self.base / name) for name in ('left', 'right')}
        for device in self.devices.values():
            Path(device).touch()
        self.config = {'wrist_cameras': {name: {'device': device} for name, device in self.devices.items()}}

    def tearDown(self):
        self.temp.cleanup()

    def test_all_controls_are_queried_before_the_first_write(self):
        fake = Controls({device: False for device in self.devices.values()})
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            reports = settings.configure_devices(self.config, True)
        self.assertEqual([args[2] for args in fake.calls[:2]], list(self.devices.values()))
        self.assertTrue(all(args[3].startswith('--get-ctrl=') for args in fake.calls[:2]))
        self.assertTrue(all(report['applied'] for report in reports.values()))
        self.assertTrue(all(report['previous_actual_allow_auto_exposure_fps_change'] is False for report in reports.values()))
        self.assertFalse((self.base / 'runtime' / settings.PREFERENCES_FILE).exists())

    def test_unsupported_second_camera_prevents_any_writes(self):
        fake = Controls({self.devices['left']: False}, unsupported=[self.devices['right']])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            with self.assertRaises(settings.WristCameraSettingsError) as error:
                settings.configure_devices(self.config, True)
        self.assertEqual(fake.writes, [])
        self.assertEqual(set(error.exception.reports), {'left', 'right'})

    def test_second_readback_failure_rolls_back_first_and_does_not_save(self):
        settings.save(self.base, None)
        fake = Controls({device: False for device in self.devices.values()}, ignore_set=[self.devices['right']])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            with self.assertRaises(settings.WristCameraSettingsError) as error:
                settings.configure_devices(self.config, True)
        self.assertEqual(fake.values, {device: False for device in self.devices.values()})
        reports = error.exception.reports
        self.assertTrue(reports['left']['rolled_back'])
        self.assertTrue(reports['right']['rolled_back'])
        self.assertFalse(reports['left']['applied'])
        self.assertEqual(settings.load(self.base), {settings.KEY: None})

    def test_failed_rollback_is_reported_without_claiming_restored(self):
        fake = Controls({device: False for device in self.devices.values()},
                        ignore_set=[self.devices['right']], failed_rollback=[self.devices['left']])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            with self.assertRaises(settings.WristCameraSettingsError) as error:
                settings.configure_devices(self.config, True)
        left = error.exception.reports['left']
        self.assertFalse(left['rolled_back'])
        self.assertIn('rollback', left['rollback_error'])
        self.assertIs(left['actual_allow_auto_exposure_fps_change'], True)

    def test_no_devices_and_disconnected_slot_are_pending_and_never_written(self):
        for device in self.devices.values():
            Path(device).unlink()
        with patch.object(settings.subprocess, 'run') as run:
            reports = settings.configure_devices(self.config, False)
            self.assertEqual(settings.configure_devices({'wrist_cameras': {}}, True), {})
        run.assert_not_called()
        for report in reports.values():
            self.assertTrue(report['pending'])
            self.assertFalse(report['applied'])
            self.assertIsNone(report['actual_allow_auto_exposure_fps_change'])
            self.assertIsNone(report['previous_actual_allow_auto_exposure_fps_change'])
        Path(self.devices['left']).touch()
        self.config['wrist_cameras']['left']['discovery'] = {'present': False}
        with patch.object(settings.subprocess, 'run') as run:
            settings.configure_devices(self.config, True)
        run.assert_not_called()

    def test_restore_uses_actual_snapshot_when_original_preference_was_none(self):
        settings.save(self.base, None)
        fake = Controls({device: True for device in self.devices.values()})
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            reports = settings.configure_devices(self.config, False)
            self.assertTrue(all(item['previous_actual_allow_auto_exposure_fps_change'] is True for item in reports.values()))
            self.assertTrue(all(value is False for value in fake.values.values()))
            restored = settings.restore_devices(reports)
        self.assertTrue(all(value is True for value in fake.values.values()))
        self.assertTrue(all(item['applied'] for item in restored.values()))
        self.assertEqual(settings.load(self.base), {settings.KEY: None})

    def test_restore_failure_returns_all_results_and_attempts_other_camera(self):
        fake = Controls({device: True for device in self.devices.values()}, failed_rollback=[self.devices['left']])
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            reports = settings.configure_devices(self.config, False)
            with self.assertRaisesRegex(settings.WristCameraSettingsError, 'Failed to restore') as error:
                settings.restore_devices(reports)
        restored = error.exception.reports
        self.assertFalse(restored['left']['applied'])
        self.assertIs(restored['left']['actual_allow_auto_exposure_fps_change'], False)
        self.assertTrue(restored['right']['applied'])
        self.assertIs(fake.values[self.devices['right']], True)

    def test_missing_devices_restore_is_skipped_without_false_verification(self):
        reports = {'missing': settings.unavailable_report({'device': '/dev/missing'}, True)}
        with patch.object(settings.subprocess, 'run') as run:
            restored = settings.restore_devices(reports)
        run.assert_not_called()
        self.assertTrue(restored['missing']['rollback_skipped'])
        self.assertIsNone(restored['missing']['actual_allow_auto_exposure_fps_change'])

    def test_preserve_transaction_and_restore_never_write_controls(self):
        fake = Controls({device: True for device in self.devices.values()})
        with patch.object(settings.subprocess, 'run', side_effect=fake):
            reports = settings.configure_devices(self.config, None)
            restored = settings.restore_devices(reports)
        self.assertEqual(fake.writes, [])
        self.assertTrue(all(item['rollback_skipped'] for item in restored.values()))


def fake_gi():
    gi = ModuleType('gi')
    gi.require_version = Mock()
    repository = ModuleType('gi.repository')
    pipeline = Mock()
    Gst = SimpleNamespace(init=Mock(), parse_launch=Mock(return_value=pipeline),
                          State=SimpleNamespace(PLAYING=1, NULL=0),
                          StateChangeReturn=SimpleNamespace(FAILURE=-1))
    pipeline.set_state.return_value = 1
    repository.Gst = Gst
    return {'gi': gi, 'gi.repository': repository}, Gst, pipeline


class SourceIntegrationTests(unittest.TestCase):
    def config(self):
        return dict(device='/dev/fake', width=640, height=480, fps=30, fourcc='MJPG', **{settings.KEY: False})

    def test_control_is_verified_before_pipeline_open_and_report_is_retained(self):
        modules, Gst, pipeline = fake_gi()
        order = []
        report = dict(device='/dev/fake', requested=False, actual_allow_auto_exposure_fps_change=False,
                      supported=True, applied=True, error=None, time=123.)
        def configure(config):
            order.append('configure')
            return report
        def launch(description):
            order.append('pipeline')
            return pipeline
        Gst.parse_launch.side_effect = launch
        with patch.dict(sys.modules, modules), patch('pathlib.Path.exists', return_value=True), \
                patch.object(settings, 'configure_device', side_effect=configure):
            source = UvcSource(self.config(), Mock())
        self.assertEqual(order, ['configure', 'pipeline'])
        self.assertIs(source.camera_settings, report)
        self.assertIn('framerate=30/1', source.description)
        source.close()

    def test_control_failure_prevents_pipeline_and_preserves_readback_report(self):
        modules, Gst, pipeline = fake_gi()
        report = dict(device='/dev/fake', requested=False, actual_allow_auto_exposure_fps_change=True,
                      supported=True, applied=False, error='mismatch', time=123.)
        failure = settings.WristCameraSettingsError('mismatch', report)
        with patch.dict(sys.modules, modules), patch('pathlib.Path.exists', return_value=True), \
                patch.object(settings, 'configure_device', side_effect=failure):
            with self.assertRaises(settings.WristCameraSettingsError) as error:
                UvcSource(self.config(), Mock())
        Gst.parse_launch.assert_not_called()
        self.assertIs(error.exception.report['actual_allow_auto_exposure_fps_change'], True)
        self.assertFalse(error.exception.report['applied'])

    def test_pipeline_failure_keeps_the_successful_control_measurement(self):
        modules, Gst, pipeline = fake_gi()
        pipeline.set_state.return_value = -1
        report = dict(device='/dev/fake', requested=False, actual_allow_auto_exposure_fps_change=False,
                      supported=True, applied=True, error=None, time=123.)
        with patch.dict(sys.modules, modules), patch('pathlib.Path.exists', return_value=True), \
                patch.object(settings, 'configure_device', return_value=report):
            with self.assertRaisesRegex(RuntimeError, 'pipeline cannot start') as error:
                UvcSource(self.config(), Mock())
        self.assertIs(error.exception.report['actual_allow_auto_exposure_fps_change'], False)
        self.assertIn('pipeline cannot start', error.exception.report['error'])


def load_isolated_module(relative, modules):
    path = Path(__file__).resolve().parents[1] / 'src/orangepi_collect' / relative
    name = 'wrist_test_' + path.stem
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def runtime_modules(config):
    modules, Gst, pipeline = fake_gi()
    Gst.PadProbeType = SimpleNamespace(BUFFER=1)
    Gst.MessageType = SimpleNamespace(EOS=8, ERROR=16)
    Gst.SECOND = 1000000000
    pipeline.get_bus.return_value.timed_pop_filtered.return_value = SimpleNamespace(type=8)
    paths = ModuleType('project_paths')
    paths.devices = Mock(return_value=config)
    rospy = ModuleType('rospy')
    rospy.init_node = Mock()
    rospy.signal_shutdown = Mock()
    rospy.Subscriber = Mock()
    sensor = ModuleType('sensor_msgs')
    sensor_msg = ModuleType('sensor_msgs.msg')
    sensor_msg.CompressedImage = object
    live = ModuleType('live_preview')
    live.publisher = Mock(return_value=Mock())
    modules.update(project_paths=paths, rospy=rospy, sensor_msgs=sensor,
                   **{'sensor_msgs.msg': sensor_msg, 'live_preview': live})
    return modules, Gst, pipeline


class ReportIntegrationTests(unittest.TestCase):
    def report(self, actual=False, applied=True):
        return dict(device='/dev/fake', requested=False, actual_allow_auto_exposure_fps_change=actual,
                    supported=True, applied=applied, error=None, time=123.)

    def config(self):
        return {'wrist_cameras': {'cam_left_wrist': dict(device='/dev/fake', width=640,
                height=480, fps=30, fourcc='MJPG', **{settings.KEY: False})}}

    def test_preview_reports_success_failure_and_disconnected_slot_atomically(self):
        config = self.config()
        config['wrist_cameras']['failed'] = dict(config['wrist_cameras']['cam_left_wrist'], device='/dev/failed')
        config['wrist_cameras']['missing'] = dict(config['wrist_cameras']['cam_left_wrist'], discovery={'present': False})
        modules, Gst, pipeline = runtime_modules(config)
        preview = load_isolated_module('acquisition/preview_worker.py', modules)
        report = self.report()
        source = Mock(camera_settings=report)
        failure = settings.WristCameraSettingsError('readback mismatch', self.report(actual=True, applied=False))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORANGEPI_PREVIEW_DIR': tmp}), \
                patch.dict(sys.modules, modules), patch.object(preview.signal, 'signal'), \
                patch.object(preview.threading, 'Event', return_value=Mock(wait=Mock(return_value=True))), \
                patch('orangepi_collect.recorders.camera.uvc_source.UvcSource', side_effect=[source, failure]):
            preview.main(config)
            good = json.loads((Path(tmp) / 'wrist-controls-cam_left_wrist.json').read_text())
            failed = json.loads((Path(tmp) / 'wrist-controls-failed.json').read_text())
            missing = json.loads((Path(tmp) / 'wrist-controls-missing.json').read_text())
            self.assertEqual(good, report)
            self.assertIs(failed['actual_allow_auto_exposure_fps_change'], True)
            self.assertFalse(failed['applied'])
            self.assertEqual(failed['error'], 'readback mismatch')
            self.assertTrue(missing['pending'])
            self.assertFalse(list(Path(tmp).glob('*.tmp')))
        source.close.assert_called_once()

    def test_recorder_persists_controls_and_includes_them_in_close_summary(self):
        config = self.config()
        modules, Gst, pipeline = runtime_modules(config)
        recorder = load_isolated_module('recorders/camera/mpp_camera_recorder.py', modules)
        report = self.report()
        source = Mock(camera_settings=report, description='unchanged source pipeline',
                      last_invalid_frame=None,invalid_preparation_frames=0)
        probe = dict(streams=[dict(codec_name='hevc', width=640, height=480, nb_read_packets='1', duration='0.03')])
        result = SimpleNamespace(returncode=0, stdout=json.dumps(probe).encode(), stderr=b'')
        with tempfile.TemporaryDirectory() as tmp, patch.object(recorder, 'UvcSource', return_value=source), \
                patch.object(recorder.subprocess, 'run', return_value=result):
            camera = recorder.Camera(SimpleNamespace(path=Path(tmp)), 'cam_left_wrist', usb=True)
            saved = json.loads((camera.path / 'camera_controls.json').read_text())
            self.assertEqual(saved, report)
            camera.accepted = camera.encoded = 1
            summary = camera.close()
            self.assertEqual(summary['camera_controls'], report)
            self.assertEqual(summary['fps'], 30)
        source.close.assert_called_once()

    def test_recorder_startup_failure_persists_failed_readback(self):
        config = self.config()
        modules, Gst, pipeline = runtime_modules(config)
        recorder = load_isolated_module('recorders/camera/mpp_camera_recorder.py', modules)
        failure = settings.WristCameraSettingsError('readback mismatch', self.report(actual=True, applied=False))
        with tempfile.TemporaryDirectory() as tmp, patch.object(recorder, 'UvcSource', side_effect=failure):
            with self.assertRaises(settings.WristCameraSettingsError):
                recorder.Camera(SimpleNamespace(path=Path(tmp)), 'cam_left_wrist', usb=True)
            saved = json.loads((Path(tmp) / 'cam_left_wrist' / 'camera_controls.json').read_text())
            self.assertIs(saved['actual_allow_auto_exposure_fps_change'], True)
            self.assertFalse(saved['applied'])
            self.assertEqual(saved['error'], 'readback mismatch')


if __name__ == '__main__':
    unittest.main()
