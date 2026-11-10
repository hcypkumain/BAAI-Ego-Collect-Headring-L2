"""Exercise the deployed camera shutdown and stop-receipt protocol without hardware."""
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src/orangepi_collect'
CAMERA_FILE = SRC / 'recorders/camera/mpp_camera_recorder.py'
UVC_FILE = SRC / 'recorders/camera/uvc_source.py'
CAMD_FILE = SRC / 'recorders/camera/camd_source.py'
CAPTURE_FILE = SRC / 'acquisition/capture.py'
FRAMEWORK_FILE = SRC / 'framework/framework_recorder.py'
PRIVACY_FILE = SRC / 'common/screen_privacy.py'


def load_function(path, name, space):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(path), 'exec'), space)
    return space[name]


def load_method(path, class_name, name, space):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(path), 'exec'), space)
    return space[name]


def test_atomic_json(path, value):
    """Filesystem injection for Windows; publisher semantics remain real."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


class CameraDataError(RuntimeError):
    pass


class GstStub:
    SECOND = 1
    State = types.SimpleNamespace(NULL=0, VOID_PENDING=1, READY=2)
    StateChangeReturn = types.SimpleNamespace(SUCCESS=0, FAILURE=1)
    FlowReturn = types.SimpleNamespace(OK=0, ERROR=1)
    MessageType = types.SimpleNamespace(EOS=1, ERROR=2)


class FakeThread:
    def __init__(self, alive_after_join=False):
        self.alive = True
        self.alive_after_join = alive_after_join

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.alive = self.alive_after_join


class FakePipeline:
    def __init__(self, current=GstStub.State.NULL, transition=GstStub.StateChangeReturn.SUCCESS):
        self.current = current
        self.transition = transition
        self.calls = []

    def set_state(self, state):
        self.calls.append(state)
        if self.transition != GstStub.StateChangeReturn.FAILURE:
            self.current = state
        return self.transition

    def get_state(self, timeout):
        return GstStub.StateChangeReturn.SUCCESS, self.current, GstStub.State.VOID_PENDING


class FakeQueue:
    def put(self, value, timeout=None):
        self.value = value

    def stats(self):
        return {}


class FakeSource:
    def __init__(self, result=GstStub.FlowReturn.OK):
        self.result = result

    def emit(self, event):
        self.event = event
        return self.result


class FakeBus:
    def __init__(self, message):
        self.message = message

    def timed_pop_filtered(self, timeout, message_types):
        return self.message


class FakeInput:
    def __init__(self, error=None):
        self.error = error

    def close(self):
        if self.error:
            raise self.error


class FakeSubprocess:
    PIPE = subprocess.PIPE

    def __init__(self, result):
        self.result = result

    def run(self, *args, **kwargs):
        return self.result


def build_camera(close_method, tmp, *, accepted=1, bus_message=None, ffprobe_result=None,
                 thread_alive_after_join=False, pipeline_transition=GstStub.StateChangeReturn.SUCCESS,
                 pipeline_state=GstStub.State.NULL, input_error=None, source_result=GstStub.FlowReturn.OK):
    timestamp_file = open(Path(tmp) / 'timestamps.csv', 'w+', newline='')
    if ffprobe_result is None:
        ffprobe_result = subprocess.CompletedProcess([], 0,
            stdout=b'{"streams":[{"codec_name":"hevc","width":1920,"height":1080,"nb_read_packets":"1","duration":"0.03"}]}',
            stderr=b'')
    camera = types.SimpleNamespace(
        name='cam_right_wrist', usb=True, path=Path(tmp), _preview_stop=threading.Event(),
        _preview_thread=None, _preview_close_error=None, input=FakeInput(input_error),
        thread=FakeThread(thread_alive_after_join), queue=FakeQueue(), source=FakeSource(source_result),
        accepted=accepted, bus=FakeBus(bus_message or types.SimpleNamespace(type=GstStub.MessageType.EOS)),
        pipeline=FakePipeline(pipeline_state, pipeline_transition), file=timestamp_file,
        frame_gap_qc={'events': []}, encoded=accepted, capture_size=(1920, 1080),
        camera_settings=None, config={}, topic=None, fps=30, _preview_frames=0, _preview_errors=0,
        max_timestamp_flush_seconds=0.0)
    def close_with_probe(self):
        with patch.dict(close_method.__globals__, {'subprocess': FakeSubprocess(ffprobe_result)}):
            return close_method(self)
    camera.close = types.MethodType(close_with_probe, camera)
    return camera


class CameraShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.close_camera = load_method(CAMERA_FILE, 'Camera', 'close',
            {'Gst': GstStub, 'os': os, 'json': json, 'subprocess': subprocess,
             'CameraDataError': CameraDataError,
             'verify_resolution': lambda actual, expected, label: None})
        cls.close_camera = staticmethod(cls.close_camera)
        cls.close_recorders = staticmethod(load_function(CAPTURE_FILE, 'close_camera_recorders',
            {'CameraDataError': CameraDataError}))
        cls.publish_completion = staticmethod(load_function(FRAMEWORK_FILE, 'publish_capture_completion',
            {'json': json, 'time': time, 'Path': Path, 'atomic_json': test_atomic_json}))
        cls.uvc_close = staticmethod(load_method(UVC_FILE, 'UvcSource', 'close', {}))
        cls.camd_close = staticmethod(load_method(CAMD_FILE, 'CamdH264Source', 'close', {'CamdError': RuntimeError}))
        cls.preview_close = staticmethod(load_method(CAMERA_FILE, 'Camera', '_close_preview', {}))
        spec = __import__('importlib.util', fromlist=['spec_from_file_location']).spec_from_file_location(
            'cp19_screen_privacy', PRIVACY_FILE)
        cls.privacy_module = __import__('importlib.util', fromlist=['module_from_spec']).module_from_spec(spec)
        spec.loader.exec_module(cls.privacy_module)

    def make_privacy(self, proof, committed_ns):
        privacy = self.privacy_module.ScreenPrivacy()
        privacy.phase = 'recording'
        privacy.committed = True
        privacy.committed_ns = committed_ns
        privacy.start_proof = proof
        return privacy

    def run_data_failure_to_receipt(self, scenario):
        with tempfile.TemporaryDirectory() as tmp:
            bus_message = None
            probe_result = None
            accepted = 1
            if scenario == 'zero_frames':
                accepted = 0
            elif scenario == 'eos_error':
                bus_message = types.SimpleNamespace(type=GstStub.MessageType.ERROR,
                    parse_error=lambda: ('stream error', 'encoder did not finish'))
            elif scenario == 'ffprobe_error':
                probe_result = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'bad mp4')
            camera = build_camera(self.close_camera, tmp, accepted=accepted,
                bus_message=bus_message, ffprobe_result=probe_result)
            error_list, summary = [], {}
            shutdown = self.close_recorders([camera], True, error_list, summary, True)
            self.assertTrue(camera.file.closed)
            self.assertTrue(camera.pipeline.calls)
            self.assertEqual(camera.pipeline.current, GstStub.State.NULL)
            self.assertTrue(shutdown)
            self.assertTrue(error_list)
            self.assertEqual(summary, {})

            stop_ns = time.monotonic_ns() - 1
            proof = {'schema_version': 1, 'policy': 'screen_black_before_capture_v1',
                     'nonce': 'test-nonce', 'session_id': 'test-session',
                     'worker_pid': 4312, 'boot_id': 'test-boot'}
            gate = {'stop_monotonic_ns': stop_ns, 'recording_active': False,
                    'screen_privacy': proof, 'worker_pid': 4312, 'boot_id': 'test-boot'}
            (Path(tmp) / 'stop_gate.json').write_text(json.dumps(gate), encoding='utf-8')
            capture_summary = {'shutdown_complete': shutdown, 'status': 'error',
                               'stop_monotonic_ns': stop_ns}
            process = types.SimpleNamespace(pid=4312, poll=lambda: 1)
            self.assertEqual(self.publish_completion(Path(tmp), process, capture_summary), True)
            receipt = json.loads((Path(tmp) / 'capture_complete.json').read_text(encoding='utf-8'))
            self.assertEqual(receipt['summary_status'], 'error')
            self.assertEqual(receipt['native_exit_code'], 1)
            self.assertTrue(receipt['native_reaped'])
            self.assertTrue(receipt['shutdown_complete'])
            privacy = self.make_privacy(proof, stop_ns - 1)
            self.assertTrue(privacy.confirm_stopped(receipt))
            self.assertTrue(privacy.snapshot()['stop_confirmed'])
            self.assertEqual(capture_summary['status'], 'error')
            self.assertEqual(1 if error_list else 0, 1)

    def test_zero_frames_still_releases_privacy_after_data_error(self):
        self.run_data_failure_to_receipt('zero_frames')

    def test_normal_close_and_exit_zero_preserve_success_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera=build_camera(self.close_camera,tmp)
            errors,summary=[],{}
            shutdown=self.close_recorders([camera],True,errors,summary,True)
            self.assertTrue(shutdown, errors)
            self.assertEqual(errors,[])
            self.assertEqual(summary['cam_right_wrist']['frames'],1)
            stop_ns=time.monotonic_ns()-1
            proof={'schema_version':1,'policy':'screen_black_before_capture_v1',
                   'nonce':'success-nonce','session_id':'success-session',
                   'worker_pid':4312,'boot_id':'success-boot'}
            gate={'stop_monotonic_ns':stop_ns,'recording_active':False,
                  'screen_privacy':proof,'worker_pid':4312,'boot_id':'success-boot'}
            (Path(tmp)/'stop_gate.json').write_text(json.dumps(gate),encoding='utf-8')
            capture_summary={'shutdown_complete':True,'status':'success','stop_monotonic_ns':stop_ns}
            process=types.SimpleNamespace(pid=4312,poll=lambda:0)
            self.assertTrue(self.publish_completion(Path(tmp),process,capture_summary))
            receipt=json.loads((Path(tmp)/'capture_complete.json').read_text(encoding='utf-8'))
            self.assertEqual(receipt['native_exit_code'],0)
            self.assertEqual(receipt['summary_status'],'success')
            privacy=self.make_privacy(proof,stop_ns-1)
            self.assertTrue(privacy.confirm_stopped(receipt))
            self.assertTrue(privacy.snapshot()['stop_confirmed'])

    def test_encoder_eos_error_still_releases_privacy_after_data_error(self):
        self.run_data_failure_to_receipt('eos_error')

    def test_ffprobe_error_still_releases_privacy_after_data_error(self):
        self.run_data_failure_to_receipt('ffprobe_error')

    def test_live_feeder_keeps_shutdown_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera = build_camera(self.close_camera, tmp, accepted=0, thread_alive_after_join=True)
            errors, summary = [], {}
            shutdown = self.close_recorders([camera], True, errors, summary, True)
            self.assertFalse(shutdown)
            self.assertTrue(any('did not stop' in error for error in errors))
            self.assertTrue(camera.file.closed)
            self.assertFalse(self.publish_completion(Path(tmp), types.SimpleNamespace(pid=4312, poll=lambda: 1),
                {'shutdown_complete': shutdown, 'status': 'error', 'stop_monotonic_ns': 1}))
            self.assertFalse((Path(tmp) / 'capture_complete.json').exists())

    def test_state_transition_failure_keeps_shutdown_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera = build_camera(self.close_camera, tmp, accepted=0,
                pipeline_transition=GstStub.StateChangeReturn.FAILURE)
            errors, summary = [], {}
            shutdown = self.close_recorders([camera], True, errors, summary, True)
            self.assertFalse(shutdown)
            self.assertTrue(any('refused NULL' in error for error in errors))

    def test_real_privacy_validator_rejects_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop_ns = time.monotonic_ns() - 1
            proof = {'nonce': 'test-nonce', 'session_id': 'test-session',
                     'worker_pid': 4312, 'boot_id': 'test-boot'}
            receipt = {'stop_monotonic_ns': stop_ns, 'finalized_monotonic_ns': time.monotonic_ns(),
                'native_reaped': True, 'native_exit_code': 1, 'capture_finalized': True,
                'shutdown_complete': True, 'summary_status': 'error', 'recording_active': False,
                'screen_privacy': proof, 'worker_pid': 4312, 'boot_id': 'test-boot'}
            for field, value in (('worker_pid', 4313), ('boot_id', 'other-boot'),
                                 ('native_reaped', False), ('native_exit_code', -1),
                                 ('capture_finalized', False), ('shutdown_complete', False),
                                 ('recording_active', True)):
                privacy = self.make_privacy(proof, stop_ns - 1)
                changed = dict(receipt, **{field: value})
                self.assertFalse(privacy.confirm_stopped(changed), field)
            privacy = self.make_privacy(proof, stop_ns - 1)
            changed_proof = dict(proof, nonce='other-nonce')
            self.assertFalse(privacy.confirm_stopped(dict(receipt, screen_privacy=changed_proof)))

    def test_uvc_close_requires_confirmed_null_state(self):
        good = types.SimpleNamespace(
            set_state=lambda state: GstStub.StateChangeReturn.SUCCESS,
            get_state=lambda timeout: (GstStub.StateChangeReturn.SUCCESS,
                                       GstStub.State.NULL, GstStub.State.VOID_PENDING))
        self.uvc_close(types.SimpleNamespace(pipeline=good,
            Gst=types.SimpleNamespace(State=GstStub.State, StateChangeReturn=GstStub.StateChangeReturn,
                                     SECOND=GstStub.SECOND)))
        bad = types.SimpleNamespace(
            set_state=lambda state: GstStub.StateChangeReturn.SUCCESS,
            get_state=lambda timeout: (GstStub.StateChangeReturn.SUCCESS,
                                       GstStub.State.READY, GstStub.State.VOID_PENDING))
        with self.assertRaisesRegex(RuntimeError, 'did not reach NULL'):
            self.uvc_close(types.SimpleNamespace(pipeline=bad,
                Gst=types.SimpleNamespace(State=GstStub.State, StateChangeReturn=GstStub.StateChangeReturn,
                                         SECOND=GstStub.SECOND)))

    def test_camd_close_requires_reader_thread_exit(self):
        connection = types.SimpleNamespace(send=lambda value: None, close=lambda: None)
        self.camd_close(types.SimpleNamespace(camera='left', _stop=threading.Event(),
            _connection=connection, _thread=FakeThread()))
        with self.assertRaisesRegex(RuntimeError, 'reader did not stop'):
            self.camd_close(types.SimpleNamespace(camera='left', _stop=threading.Event(),
                _connection=connection, _thread=FakeThread(alive_after_join=True)))

    def test_preview_close_requires_inner_thread_and_pipeline_to_stop(self):
        def preview(thread, pipeline):
            return types.SimpleNamespace(thread=thread, pipeline=pipeline,
                Gst=types.SimpleNamespace(SECOND=GstStub.SECOND, State=GstStub.State,
                    StateChangeReturn=GstStub.StateChangeReturn),
                close=lambda: thread.join(2))
        good_thread=FakeThread()
        good_pipeline=FakePipeline()
        self.preview_close(types.SimpleNamespace(_preview_close_error=None), preview(good_thread,good_pipeline))
        live_thread=FakeThread(alive_after_join=True)
        owner=types.SimpleNamespace(_preview_close_error=None)
        with self.assertRaisesRegex(RuntimeError, 'feeder thread did not stop'):
            self.preview_close(owner,preview(live_thread,FakePipeline()))
        self.assertIsNotNone(owner._preview_close_error)
        owner=types.SimpleNamespace(_preview_close_error=None)
        with self.assertRaisesRegex(RuntimeError, 'pipeline did not reach NULL'):
            self.preview_close(owner,preview(FakeThread(),FakePipeline(current=GstStub.State.READY)))
        self.assertIsNotNone(owner._preview_close_error)


if __name__ == '__main__':
    unittest.main()
