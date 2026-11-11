"""Fault injection for frozen hardware contracts and saved capture evidence.

These tests use synthetic configuration and temporary files only.  They do not
open hardware, perform a capture, or contact an upload service.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

COMMON = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'
sys.path.insert(0, str(COMMON))
CONTROL = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control'
sys.path.insert(0, str(CONTROL))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import required_device_contract as contract
from required_capture_fixture import write_session


def complete_config(mode='emg'):
    value = {
        'collection_mode': mode,
        'cameras': {name: {'camd': name} for name in ('left', 'right', 'bleft', 'bright')},
        'wrist_cameras': {name: {'device': '/dev/fake-' + name}
                          for name in contract.WRISTS},
        'emg': {name: {'port': '/dev/fake-' + name} for name in contract.EMG},
        'board_imu': {'device': '/dev/fake-board-imu'},
    }
    if mode == 'eeg':
        value['eeg'] = {'enabled': True}
    return value


def live_evidence(names, *, stale=None):
    stale = stale or set()
    rows = {name: {'ready': True, 'fresh': name not in stale,
                   'age_seconds': 0.0, 'last_seen_monotonic': 1.0} for name in names}
    missing=sorted(set(stale)&set(names))
    return {'ready':not missing,'missing':missing,'devices':rows}


class FrozenContractFaults(unittest.TestCase):
    def test_profile_inventory_is_independent_of_a_partial_scan(self):
        config = complete_config()
        required = list(contract.required_names(config, 'emg'))
        self.assertEqual(len(required), 9)
        online = [name for name in required if not name.startswith('cam_ros_')]
        missing = contract.validate_scan(
            {'required_devices': required},
            [{'name': name, 'connected': True, 'status': 'ready'} for name in online],
        )
        self.assertEqual(set(missing), set(required) - set(online))
        self.assertEqual(len(missing), 4)

    def test_selected_subset_cannot_shrink_expected_profile_contract(self):
        config = complete_config()
        required = list(contract.required_names(config, 'emg'))
        selected = [name for name in required if not name.startswith('cam_ros_')]
        with self.assertRaisesRegex(ValueError, '缺少必需设备'):
            contract.build_contract(config, selected, 'emg')

    def test_forged_or_incomplete_frozen_contract_is_rejected(self):
        config = complete_config()
        required = list(contract.required_names(config, 'emg'))
        frozen = contract.build_contract(config, required, 'emg')
        selection = {'selected': required, 'collection_mode': 'emg',
                     'required_device_contract': dict(frozen,
                         required_devices=frozen['required_devices'][:-1])}
        resolved, errors = contract.validate_frozen(config, selection)
        self.assertIsNone(resolved)
        self.assertTrue(errors)
        selection.pop('required_device_contract')
        resolved, errors, legacy = contract.resolve_selection(config, selection)
        self.assertFalse(errors)
        self.assertTrue(legacy)
        self.assertEqual(resolved['required_devices'], required)

    def test_execution_gap_is_detected_even_when_selection_claims_full_set(self):
        names = list(contract.required_names(complete_config(), 'emg'))
        missing = contract.execution_missing(
            {'required_devices': names}, cameras=('left', 'right', 'bleft'),
            with_emg=True, with_board=True, emg_names=contract.EMG,
            wrist_names=contract.WRISTS, with_eeg=False)
        self.assertEqual(missing, ['cam_ros_bright'])


class SavedEvidenceFaults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.required = ['cam_ros_left']
        self.frozen = {'version': contract.CONTRACT_VERSION, 'mode': 'emg',
                       'required_devices': self.required,
                       'selected_devices': self.required}
        stream = self.root / 'cam_ros_left'
        stream.mkdir()
        (stream / 'frames.mp4').write_bytes(b'video')
        (stream / 'timestamps.csv').write_text('timestamp\n1\n')
        self.summary = {
            'status': 'success', 'errors': [], 'cam_ros_left': {'frames': 1},
            'required_device_start': live_evidence(self.required),
            'required_device_stop': live_evidence(self.required),
            'required_device_contract_version':contract.CONTRACT_VERSION,
            'required_device_contract':self.frozen,'required_devices':self.required,
            'start_unix_ns':1_800_000_000_000_000_000,'stop_unix_ns':1_800_000_010_000_000_000,
            'start_monotonic_ns':1_000_000_000,'stop_monotonic_ns':11_000_000_000,
        }
        (self.root/'start_gate.json').write_text(json.dumps({
            'start_unix_ns':self.summary['start_unix_ns'],'start_monotonic_ns':self.summary['start_monotonic_ns'],
            'required_device_contract_version':contract.CONTRACT_VERSION,
            'required_devices':self.required,'required_device_readiness':self.summary['required_device_start']['devices']}))
        (self.root/'stop_gate.json').write_text(json.dumps({
            'stop_unix_ns':self.summary['stop_unix_ns'],'stop_monotonic_ns':self.summary['stop_monotonic_ns'],
            'recording_active':False}))

    def test_nominal_minimal_camera_evidence_is_accepted(self):
        self.assertEqual(contract.data_errors(self.root, self.summary, self.frozen), [])

    def test_failed_or_interrupted_native_close_never_looks_complete(self):
        for status, errors in (('error', []), ('success', ['native stop failed'])):
            with self.subTest(status=status, errors=errors):
                failed = dict(self.summary, status=status, errors=errors)
                self.assertTrue(contract.data_errors(self.root, failed, self.frozen))

    def test_stale_start_or_stop_evidence_blocks_completeness(self):
        for key in ('required_device_start', 'required_device_stop'):
            with self.subTest(key=key):
                broken = dict(self.summary)
                broken[key] = live_evidence(self.required, stale={'cam_ros_left'})
                self.assertTrue(contract.data_errors(self.root, broken, self.frozen))

    def test_summary_count_cannot_hide_empty_or_missing_stream_files(self):
        frame_file = self.root / 'cam_ros_left' / 'frames.mp4'
        frame_file.write_bytes(b'')
        self.assertTrue(contract.data_errors(self.root, self.summary, self.frozen))
        frame_file.write_bytes(b'video')
        (self.root / 'cam_ros_left' / 'timestamps.csv').unlink()
        self.assertTrue(contract.data_errors(self.root, self.summary, self.frozen))

    def test_missing_required_evidence_or_directory_blocks_completeness(self):
        del self.summary['required_device_stop']
        self.assertTrue(contract.data_errors(self.root, self.summary, self.frozen))
        self.summary['required_device_stop'] = live_evidence(self.required)
        (self.root / 'cam_ros_left').rename(self.root / 'cam_ros_left.missing')
        self.assertTrue(contract.data_errors(self.root, self.summary, self.frozen))


class ValidatedUploadAndAccountingFaults(unittest.TestCase):
    """Exercise real RecordingStore and UploadManager with fake local data."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.base = self.project / 'orangepi'
        self.base.mkdir(parents=True)
        self.sessions = self.root / 'volume' / 'data' / 'shift'
        self.sessions.mkdir(parents=True)
        self.image = self.project / 'configs/environments/scene/task_1.png'
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b'fake reference')
        self.names = list(contract.required_names(complete_config(), 'emg'))
        self.path = self.sessions / 'take-001'
        self.path.mkdir()
        self.config = complete_config()
        self.frozen = contract.build_contract(self.config, self.names, 'emg')
        self.write_capture()

        class Ledger:
            def read(inner, _path):
                return {'used': {'task_1.png': {'session': str(self.path), 'status': 'success'}}}
            def valid_used(inner, _entry, _image=None):
                return True

        from recording_store import RecordingStore
        self.store = RecordingStore(
            self.base, self.project, Ledger(),
            session_provider=lambda: self.sessions,
            volume_provider=lambda: self.root / 'volume', clock=lambda: 1_800_000_000)

    def write_capture(self, *, report_level='WARN', scope='full', stale=None,
                      summary_status='success', qc_mode='immediate', qc_state='completed',
                      report_streams=True):
        stale = stale or set()
        _,summary,report=write_session(self.path,mode='emg',level=report_level,scope=scope,
            stale=stale,write_receipt=False,summary_status=summary_status)
        summary.update(qc_mode=qc_mode,qc_state=qc_state,qc_level=report_level)
        if not report_streams:report['streams']={}
        (self.path/'capture_summary.json').write_text(json.dumps(summary,allow_nan=False),encoding='utf-8')
        (self.path/'qc_report.json').write_text(json.dumps(report,allow_nan=False),encoding='utf-8')
        meta = {'status': summary_status, 'retention_state': 'kept',
                'qc_state': qc_state, 'qc_mode': qc_mode, 'qc_level': report_level,
                'reference_image': 'configs/environments/scene/task_1.png',
                'session_created_unix': 1_800_000_000, 'duration': 10}
        (self.path / 'meta.yaml').write_text(yaml.safe_dump(meta), encoding='utf-8')
        if (summary_status=='success' and scope=='full' and report_streams and not stale
                and report_level in ('INFO','WARN')):
            from capture_qc_receipt import write as issue
            try:issue(self.path)
            except (OSError,ValueError,TypeError,KeyError,RecursionError):pass

    def current(self):
        return self.store.metadata(self.path)

    def manager(self):
        import cloud_uploads
        manager = cloud_uploads.UploadManager(
            self.base, self.store,
            lambda: {'state': 'idle', 'privacy': {'required': False}, 'worker': {}},
            autostart=False)
        self.addCleanup(manager.close)
        manager.set_credentials({'ak': 'local-test-ak', 'sk': 'local-test-sk'})
        return manager

    def test_full_info_warn_and_finished_deferred_qc_are_qualified(self):
        for level, mode, state in (('INFO', 'immediate', 'completed'),
                                   ('WARN', 'deferred', 'completed')):
            with self.subTest(level=level, mode=mode):
                self.write_capture(report_level=level, qc_mode=mode, qc_state=state)
                value = self.current()
                self.assertTrue(value['retained'])
                self.assertTrue(value['qualified'], value['qualification_error'])

    def test_receipt_binds_full_report_and_stream_files(self):
        self.write_capture()
        from capture_qc_receipt import verify
        self.assertEqual(verify(self.path), [])
        video=self.path/'cam_ros_left'/'frames.mp4'
        video.write_bytes(b'changed video contents')
        failures=verify(self.path)
        self.assertTrue(any('changed' in item or '变化' in item for item in failures),failures)
        self.assertFalse(self.current()['qualified'])

    def test_local_partial_or_incomplete_full_report_is_not_qualified(self):
        for scope, report_streams in (('emg_only', True), ('full', False)):
            with self.subTest(scope=scope, report_streams=report_streams):
                self.write_capture(scope=scope, report_streams=report_streams)
                self.assertFalse(self.current()['qualified'])

    def test_stale_required_stream_and_failed_verdict_cannot_qualify(self):
        self.write_capture(stale={'cam_ros_left'})
        self.assertFalse(self.current()['qualified'])
        self.write_capture(report_level='ERROR')
        self.assertFalse(self.current()['qualified'])

    def test_retained_error_remains_diagnostic_only_and_never_standard_upload(self):
        self.write_capture(report_level='ERROR', summary_status='qc_error')
        value = self.current()
        self.assertTrue(value['retained'])
        self.assertFalse(value['qualified'])
        self.assertEqual(value['upload_state'], 'diagnostic_only')
        self.assertEqual(self.store.upload_manifest('all')['recordings'], [])
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, '未达到标准上传条件'):
            manager.enqueue({'selection': [{'session_id': self.path.name, 'token': value['token']}],
                             'settings': {'project_name': 'EGOL2', 'target_parent': 'EGOL2/test', 'task_id': None}})

    def test_queue_rejects_changed_qc_proof_before_enqueue_and_before_launch(self):
        original = self.current()
        manager = self.manager()
        request = {'selection': [{'session_id': self.path.name, 'token': original['token']}],
                   'settings': {'project_name': 'EGOL2', 'target_parent': 'EGOL2/test', 'task_id': None}}
        # Replacing the report after the phone list was read invalidates its token.
        report_path = self.path / 'qc_report.json'
        report = json.loads(report_path.read_text())
        report['scope'] = 'emg_only'
        report_path.write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            manager.enqueue(request)

        # A take qualified at enqueue is re-checked before launching a worker.
        self.write_capture()
        fresh = self.current()
        request['selection'][0]['token'] = fresh['token']
        queued = manager.enqueue(request)
        job = next(item for item in manager.state['jobs'] if item['id'] == queued['queued'][0])
        report = json.loads(report_path.read_text())
        report['streams'].pop(self.names[0])
        report_path.write_text(json.dumps(report))
        with patch('cloud_uploads.subprocess.Popen') as spawn:
            manager._launch(job)
        self.assertEqual(job['state'], 'failed')
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
