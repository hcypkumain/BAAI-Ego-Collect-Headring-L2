"""Queue capacity, draining and STOP acceptance with a stalled camera writer."""
import ast
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

CAMERA = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/recorders/camera'
spec = importlib.util.spec_from_file_location('frame_queue', CAMERA / 'frame_queue.py')
module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
FrameQueue = module.FrameQueue


def camera_class():
    tree = ast.parse((CAMERA / 'mpp_camera_recorder.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Camera')
    # Compile actual callback without importing GStreamer on the workstation.
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'camd_callback']
    scope = dict(time=time, json=json, queue=queue)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(CAMERA / 'mpp_camera_recorder.py'), 'exec'), scope)
    return scope['Camera']


class FrameQueueTests(unittest.TestCase):
    def test_burst_after_1_5_second_writer_stall_preserves_every_frame_in_order(self):
        q = FrameQueue(120, 16 * 1024 * 1024)
        for i in range(45):q.put_nowait((b'x' * 32768, i))
        self.assertEqual(q.stats()['peak_frames'], 45)
        self.assertEqual([q.get()[1] for _ in range(45)], list(range(45)))
        self.assertEqual(q.stats()['bytes'], 0)

    def test_frame_and_byte_limits_never_drop_an_existing_item(self):
        for q, payload, count in [(FrameQueue(2, 100), b'x', 2), (FrameQueue(10, 4), b'xx', 2)]:
            for i in range(count):q.put_nowait((payload, i))
            with self.assertRaises(queue.Full):q.put_nowait((payload, 9))
            self.assertEqual([q.get()[1] for _ in range(count)], list(range(count)))
            self.assertEqual(q.stats()['bytes'], 0)
        with self.assertRaises(queue.Full):FrameQueue(10, 4).put_nowait((b'xxxxx', 0))

    def test_blocked_put_and_sentinel_drain_after_consumer_resumes(self):
        q = FrameQueue(1, 4);q.put((b'xxxx', 1));finished = threading.Event()
        t = threading.Thread(target=lambda: (q.put(None, timeout=1), finished.set()))
        t.start();self.assertFalse(finished.wait(.02))
        self.assertEqual(q.get()[1], 1);self.assertTrue(finished.wait(1));t.join(1)
        self.assertIsNone(q.get());self.assertEqual(q.stats()['bytes'], 0)

    def test_timeout_does_not_change_queue_accounting(self):
        q = FrameQueue(10, 4);q.put((b'xxxx', 1))
        with self.assertRaises(queue.Full):q.put((b'x', 2), timeout=.01)
        self.assertEqual(q.stats()['bytes'], 4)
        self.assertEqual(q.get()[1], 1)

    def test_real_camera_callback_remains_stoppable_during_writer_stall(self):
        c = camera_class()();c._h264_preview = None;c.observed_size = (1920, 1200)
        c.seen = 0;c.accepted = 1;c.name = 'cam_ros_left';c.queue = FrameQueue(120, 16 * 1024 * 1024)
        failures = []
        owner = SimpleNamespace(gate=threading.RLock(), active=True, start_ns=0, start_wall_ns=0)
        def fail(message):owner.active = False;failures.append(message)
        owner.fail = fail;c.owner = owner
        for i in range(45):c.camd_callback(b'x' * 32768, i + 1, True, i + 1, i + 1)
        self.assertEqual(c.queue.qsize(), 45);self.assertEqual(failures, [])
        with owner.gate:owner.active = False
        c.camd_callback(b'post-stop', 46, True, 46, 46)
        self.assertEqual(c.queue.qsize(), 45)
        self.assertEqual([c.queue.get()[1] for _ in range(45)], [(i + 1) * 1000 for i in range(45)])

    def test_persistent_overflow_is_still_a_capture_failure_with_evidence(self):
        c = camera_class()();c._h264_preview = None;c.observed_size = (1920, 1200)
        c.seen = 0;c.accepted = 1;c.name = 'cam_ros_left';c.queue = FrameQueue(2, 10)
        failures = [];owner = SimpleNamespace(gate=threading.RLock(), active=True, start_ns=0, start_wall_ns=0)
        def fail(message):owner.active = False;failures.append(message)
        owner.fail = fail;c.owner = owner
        for i in range(3):c.camd_callback(b'x', i + 1, True, i + 1, i + 1)
        self.assertFalse(owner.active);self.assertEqual(len(failures), 1)
        self.assertIn('camera queue overflow', failures[0]);self.assertIn('capacity_bytes', failures[0])
        self.assertEqual(c.queue.qsize(), 2)


if __name__ == '__main__':
    unittest.main()
