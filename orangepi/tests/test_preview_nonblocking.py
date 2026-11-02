"""Concurrency regressions: a slow preview must not stall acquisition callers."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import live_preview


def frame(values=None):
    values = values or [-8388608, -1, 0, 1, 8388607, 200, -300, 5]
    return b'\xd2\xd2\xd2\xaa\x00' + b''.join(x.to_bytes(3, 'big', signed=True) for x in values)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pub = live_preview.Publisher(self.temp.name)
        # Stop the periodic writer so test synchronization is explicit.
        self.pub.close()

    def tearDown(self):
        self.pub.close()
        self.temp.cleanup()

    def test_all_producers_return_while_preview_lock_is_held(self):
        done = threading.Event()
        errors = []

        def produce():
            try:
                self.pub.camera('cam_left_wrist', b'\xff\xd8test')
                self.pub.emg('emg_left', frame())
                self.pub.eeg([[0.] * 8], [True], [str(i) for i in range(8)], 'none', 100.)
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        with self.pub.lock:
            worker = threading.Thread(target=produce)
            worker.start()
            completed_under_contention = done.wait(1.)
        worker.join(2.)
        self.assertTrue(completed_under_contention, 'Producer waited for preview lock')
        self.assertFalse(errors)
        self.assertEqual(dict(self.pub.skipped_updates), {'cam_left_wrist': 1, 'emg_left': 1, 'eeg': 1})

    def test_slow_json_writer_does_not_block_new_preview_updates(self):
        self.pub.emg('emg_left', frame())
        encoding = threading.Event()
        release = threading.Event()
        original = json.dumps

        def slow_encode(*args, **kwargs):
            encoding.set()
            release.wait(2.)
            return original(*args, **kwargs)

        with patch.object(live_preview.json, 'dumps', side_effect=slow_encode):
            worker = threading.Thread(target=self.pub._publish_once)
            worker.start()
            try:
                self.assertTrue(encoding.wait(1.))
                self.assertFalse(self.pub.lock.locked())
                self.pub.emg('emg_left', frame())
                self.pub.camera('cam_right_wrist', b'\xff\xd8new')
                self.assertEqual(self.pub.signals['emg_left']['count'], 2)
                self.assertIn('cam_right_wrist.jpg', self.pub.pending)
            finally:
                release.set()
                worker.join(2.)
        self.pub._publish_once()
        self.assertEqual((Path(self.temp.name) / 'cam_right_wrist.jpg').read_bytes(), b'\xff\xd8new')

    def test_signed_extrema_count_and_bounded_history_are_preserved(self):
        for i in range(150):
            with patch.object(live_preview.time, 'time', return_value=100 + i * .025):
                self.pub.emg('emg_left', frame())
        self.pub._publish_once()
        value = json.loads((Path(self.temp.name) / 'emg_left.json').read_text())
        self.assertEqual(len(value['rows']), 100)
        self.assertEqual(value['rows'][-1][1][0], [-8388608, -8388608])
        self.assertEqual(value['count'], 150)

    def test_busy_camera_preview_can_be_retried_without_throttle(self):
        with self.pub.lock:
            self.pub.camera('cam_left_wrist', b'\xff\xd8old')
        self.pub.camera('cam_left_wrist', b'\xff\xd8new')
        self.pub._publish_once()
        self.assertEqual((Path(self.temp.name) / 'cam_left_wrist.jpg').read_bytes(), b'\xff\xd8new')

    def test_eeg_metadata_and_finite_channel_bounds_are_preserved(self):
        self.pub.eeg([[0., float('nan'), 2., 3., 4., 5., 6., 7.], [0., -1., 4., 3., 4., 5., 6., 7.]],
                     [True, True], ['time'] + ['channel' + str(i) for i in range(1, 8)], 'none', 100.)
        self.pub._publish_once()
        value = json.loads((Path(self.temp.name) / 'eeg.json').read_text())
        self.assertEqual(value['rows'][0][1][0], [-1., -1.])
        self.assertEqual(value['rows'][0][1][1], [2., 4.])
        self.assertEqual(value['physical_channels'], list(range(2, 9)))
        self.assertEqual(value['count'], 2)

    def test_no_preview_directory_has_no_file_or_signal_side_effect(self):
        with patch.dict('os.environ', {}, clear=True):
            pub = live_preview.Publisher()
        pub.camera('cam_left_wrist', b'\xff\xd8')
        pub.emg('emg_left', frame())
        pub.eeg([[0.] * 8], [True], ['x'] * 8, 'none', 100.)
        self.assertFalse(pub.pending)
        self.assertFalse(pub.signals)
        pub.close()


if __name__ == '__main__':
    unittest.main()
