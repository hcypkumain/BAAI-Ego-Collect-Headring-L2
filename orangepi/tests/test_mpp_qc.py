import sys
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import project_paths
from embodied_brain_collect.checkers import checks
from mpp_qc import video_backend


class MppQcTests(unittest.TestCase):
    def test_concurrent_hardware_failures_keep_software_fallback_serial(self):
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        active = 0
        maximum = 0
        def hardware(*args, **kwargs):
            barrier.wait(timeout=2)
            raise RuntimeError('VPU unavailable')
        def software(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.03)
            with lock:
                active -= 1
            return 'software'
        with patch.object(checks, 'decode_video', side_effect=software):
            with patch('mpp_qc.decode_mpp', side_effect=hardware):
                with video_backend({'qc_video_backend': 'auto', 'qc_mpp_workers': 2}):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda _: checks.decode_video(Path('frames.mp4'), None, None), range(2)))
        self.assertEqual(results, ['software', 'software'])
        self.assertEqual(maximum, 1)

    def test_auto_falls_back_and_restores_original_decoder(self):
        with patch.object(checks, 'decode_video', return_value='software') as original:
            with patch('mpp_qc.decode_mpp', side_effect=RuntimeError('VPU unavailable')):
                with video_backend({'qc_video_backend': 'auto'}) as traces:
                    self.assertEqual(checks.decode_video(Path('frames.mp4'), None, None), 'software')
                self.assertIs(checks.decode_video, original)
            self.assertEqual(original.call_count, 1)
            self.assertEqual(traces[0]['backend'], 'cv2')
            self.assertIn('VPU unavailable', traces[0]['fallback_reason'])

    def test_strict_benchmark_never_reports_fallback_as_mpp(self):
        original = checks.decode_video
        with patch('mpp_qc.decode_mpp', side_effect=RuntimeError('bad layout')):
            with self.assertRaisesRegex(RuntimeError, 'bad layout'):
                with video_backend({'qc_video_backend': 'mpp'}):
                    checks.decode_video(Path('frames.mp4'), None, None)
        self.assertIs(checks.decode_video, original)

    def test_full_frame_black_detects_between_keyframes(self):
        lums = np.full(180, 255.0)
        lums[3:78] = 0
        times = np.arange(180) / 30
        dec = checks.VideoDecode(file='frames.mp4', rate_hz=1,
            lums=lums[::30], diffs=np.zeros(5), t_samp=times[::30],
            black_lums=lums, black_t_samp=times, black_rate_hz=30)
        with patch.object(checks._VideoCheck, 'decode', return_value=dec):
            result = checks.BlackFrame().run(None)
        self.assertEqual(result.stats['n_checked_frames'], 180)
        self.assertAlmostEqual(result.findings[0].spans[0].dur, 2.5)

    def test_single_black_frame_is_recorded_without_changing_warn_threshold(self):
        lums = np.full(180, 255.0)
        lums[7] = 0
        dec = checks.VideoDecode(file='frames.mp4', lums=np.full(12, 255.0),
            t_samp=np.arange(12) / 2, black_lums=lums,
            black_t_samp=np.arange(180) / 30, black_rate_hz=30)
        with patch.object(checks._VideoCheck, 'decode', return_value=dec):
            result = checks.BlackFrame().run(None)
        self.assertFalse(result.findings)
        self.assertAlmostEqual(result.stats['black_segments'][0]['duration'], 1 / 30)

    def test_freeze_keeps_legacy_sampling_rate(self):
        dec = checks.VideoDecode(file='frames.mp4', rate_hz=2,
            lums=np.full(7, 100.0), diffs=np.zeros(6), t_samp=np.arange(7) / 2,
            black_lums=np.full(90, 100.0), black_t_samp=np.arange(90) / 30,
            black_rate_hz=30)
        with patch.object(checks._VideoCheck, 'decode', return_value=dec):
            result = checks.Freeze().run(None)
        self.assertAlmostEqual(result.findings[0].spans[0].dur, 3)


if __name__ == '__main__':
    unittest.main()
