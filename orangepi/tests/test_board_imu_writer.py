"""Exercise the actual BoardImu class without ROS or opening sensor devices."""
import ast
import csv
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import types
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/acquisition/capture.py'


def board_class():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'BoardImu')
    module = ast.Module(body=[cls], type_ignores=[])
    space = {'csv': csv, 'os': os, 'queue': queue, 'threading': threading, 'time': time,
             'imu_source': types.SimpleNamespace(Icm20602Source=FakeSource)}
    exec(compile(module, str(SOURCE), 'exec'), space)
    return space['BoardImu']


class FakeSource:
    device = '/dev/fake-icm'
    description = 'test ICM20602 source'

    def __init__(self, callback):
        self.callback = callback
        self.closed = False

    def close(self):
        self.closed = True

    def health(self):
        pass


def sample(stamp, sequence=1):
    return (stamp, sequence, (5., 6., 7.), (8., 9., 10.))


class ObservableGate:
    def __init__(self):
        self.lock = threading.RLock()
        self.observe = False
        self.attempted = threading.Event()

    def acquire(self, *args):
        if self.observe:
            self.attempted.set()
        return self.lock.acquire(*args)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class Owner:
    def __init__(self, path):
        self.path = Path(path)
        self.gate = threading.RLock()
        self.active = True
        self.start_ns = self.start_wall_ns = 0
        self.failures = []
        self.warnings = []

    def warn(self, text):
        self.warnings.append(text)

    def fail(self, text):
        with self.gate:
            self.active = False
            self.failures.append(text)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.owner = Owner(self.temp.name)
        self.imu = board_class()(self.owner)
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if not self.imu.file.closed:
            try: self.imu.close()
            except RuntimeError: pass
        self.temp.cleanup()

    def pause_writer(self):
        entered = threading.Event()
        original = self.imu.csv
        release = self.release
        class SlowWriter:
            def writerow(self, row):
                entered.set()
                if not release.wait(2.): raise RuntimeError('Test writer was not released')
                return original.writerow(row)
        self.imu.csv = SlowWriter()
        return entered

    def test_slow_disk_does_not_hold_common_gate_or_block_next_frame(self):
        entered = self.pause_writer()
        self.imu.callback(*sample(100, 1))
        self.assertTrue(entered.wait(1.))
        acquired = self.owner.gate.acquire(False)
        if acquired: self.owner.gate.release()
        self.assertTrue(acquired, 'Disk writer held the common acquisition gate')
        done = threading.Event()
        worker = threading.Thread(target=lambda: (self.imu.callback(*sample(200, 2)), done.set()))
        worker.start()
        try:
            self.assertTrue(done.wait(1.), 'Next callback waited for disk IO')
            self.assertEqual(self.imu.accepted, 2)
        finally:
            self.release.set()
            worker.join(2.)
        summary = self.imu.close()
        self.assertEqual(summary['frames'], 2)
        self.assertEqual(summary['accepted_frames'], 2)

    def test_stop_drains_accepted_rows_in_order_and_rejects_later_rows(self):
        for i in range(1, 101): self.imu.callback(*sample(i * 100, i))
        with self.owner.gate: self.owner.active = False
        self.imu.callback(*sample(10100, 101))
        summary = self.imu.close()
        with (self.imu.path / 'raw.csv').open(newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 101)
        self.assertEqual([int(row[0]) for row in rows[1:]], list(range(100, 10100, 100)))
        self.assertEqual([int(row[3]) for row in rows[1:]], list(range(1, 101)))
        self.assertEqual(rows[1][4:14], ['0.0', '0.0', '0.0', '0.0', '5.0', '6.0', '7.0', '8.0', '9.0', '10.0'])
        self.assertEqual(summary['frames'], summary['accepted_frames'])
        self.assertEqual(summary['frames'], 100)

    def test_overflow_is_an_explicit_failure_instead_of_silent_drop(self):
        entered = self.pause_writer()
        self.imu.callback(*sample(100))
        self.assertTrue(entered.wait(1.))
        self.imu.jobs.maxsize = 1
        self.imu.callback(*sample(200))
        self.imu.callback(*sample(300))
        self.assertFalse(self.owner.active)
        self.assertTrue(any('queue overflow' in text for text in self.owner.failures))
        self.assertEqual(self.imu.accepted, 2)
        self.release.set()
        self.assertEqual(self.imu.close()['frames'], 2)

    def test_duplicate_source_stamp_is_recorded_and_warned_with_actual_sequence(self):
        # The current collector deliberately preserves anomalous samples for
        # original-framework QC rather than failing acquisition on duplicates.
        self.imu.callback(*sample(100, 41))
        self.imu.callback(*sample(100, 42))
        self.assertTrue(self.owner.active)
        self.assertEqual(self.owner.failures, [])
        self.assertTrue(any('sequence=42' in warning for warning in self.owner.warnings))
        summary = self.imu.close()
        self.assertEqual(summary['frames'], 2)
        self.assertEqual(summary['timestamp_duplicates'], 1)
        self.assertEqual(summary['timestamp_anomaly_events'][0]['sequence'], 42)

    def test_writer_failure_reaches_owner_and_cannot_report_success(self):
        class BrokenWriter:
            def writerow(self, row): raise OSError('test disk failure')
        self.imu.csv = BrokenWriter()
        self.imu.callback(*sample(100))
        self.imu.writer.join(1.)
        self.assertFalse(self.owner.active)
        self.assertTrue(any('writer: test disk failure' in text for text in self.owner.failures))
        with self.assertRaisesRegex(RuntimeError, 'test disk failure'): self.imu.close()
        self.assertTrue(self.imu.file.closed)

    def test_callback_waiting_at_stop_does_not_cross_acceptance_boundary(self):
        gate = self.owner.gate = ObservableGate()
        with gate:
            gate.observe = True
            worker = threading.Thread(target=lambda: self.imu.callback(*sample(100)))
            worker.start()
            self.assertTrue(gate.attempted.wait(1.))
            self.owner.active = False
            gate.observe = False
        worker.join(1.)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.imu.close()['frames'], 0)

    def test_source_or_arrival_before_start_is_rejected(self):
        self.owner.start_wall_ns = 200
        self.imu.callback(*sample(100))
        self.owner.start_wall_ns = 0
        self.owner.start_ns = time.monotonic_ns() + 1000000000
        self.imu.callback(*sample(300))
        self.assertEqual(self.imu.close()['frames'], 0)


if __name__ == '__main__':
    unittest.main()
