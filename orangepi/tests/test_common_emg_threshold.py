import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from embodied_brain_collect.checkers.camera import CameraChecker
from embodied_brain_collect.checkers.checks import ts_checks
from embodied_brain_collect.checkers.emg import EmgChecker
from emg_recoverability import plan
from framework_qc import _scope_emg_checker


class OldCoreEmgChecker(EmgChecker):
    checks = [ts_checks('emg', dup_level='ERROR'), ts_checks('imu', dup_level='ERROR')]


class ReceiverWideOverride(OldCoreEmgChecker):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.checker_cfg['timestampgap'] = {'min_s': .1500005, 'mult': 1.5}


def timestamps(gap):
    t = np.arange(100, dtype=np.float64) / 1000
    t[50:] = (t[49] + gap) + np.arange(50) / 1000
    return t


class GapTest(unittest.TestCase):
    def test_core_and_pi_apply_distinct_emg_and_imu_thresholds(self):
        cfg = {'timestamp_gap': {'min_s': .1, 'mult': 1.5}}
        checkers = [EmgChecker(cfg)]
        checkers.extend(_scope_emg_checker(cls(cfg), cfg)
                        for cls in (EmgChecker, OldCoreEmgChecker, ReceiverWideOverride))
        cases = [
            (.010, .001, None, False),
            (.120, .001, 'WARN', False),
            (.150, .001, 'WARN', False),
            (.151, .001, 'ERROR', False),
            (.001, .120, None, True),
            (.120, .120, 'WARN', True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            emg = Path(directory) / 'emg_left'
            emg.mkdir()
            for checker in checkers:
                for emg_gap, imu_gap, level, imu_error in cases:
                    with self.subTest(checker=type(checker), emg=emg_gap, imu=imu_gap):
                        np.savez(emg/'data.npz', emg_timestamps=timestamps(emg_gap),
                                 imu_timestamps=timestamps(imu_gap), emg_sn=np.arange(100)*2,
                                 imu_sn=np.arange(100)*2+1)
                        findings = checker.run(emg).findings
                        levels = [f.level for f in findings if f.check == 'EmgSampleGap']
                        self.assertEqual(levels, [level] if level else [])
                        self.assertEqual(any(f.check == 'TimestampGap' and f.level == 'ERROR'
                                             for f in findings), imu_error)
            camera = Path(directory) / 'cam_ros_left'
            camera.mkdir()
            np.savez(camera/'data.npz', frames_timestamps=timestamps(.12))
            self.assertTrue(any(f.check == 'TimestampGap' and f.level == 'ERROR'
                                for f in CameraChecker(cfg).run(camera).findings))

    def test_sequence_loss_still_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'emg_left'
            path.mkdir()
            np.savez(path/'data.npz', emg_timestamps=timestamps(.001), imu_timestamps=np.arange(2)*.01,
                     emg_sn=np.arange(100)*2, imu_sn=np.arange(2)*2+1)
            self.assertTrue(any(f.check == 'SnGap' for f in EmgChecker({}).run(path).findings))

    def test_recovery_keeps_100ms_wire_and_window_edge_limits(self):
        def rebuild(emg, emg_sn, imu, imu_sn):
            return SimpleNamespace(ok=True, emg_timestamps=emg, imu_timestamps=imu,
                                   period=.001, residual_ms=0.)

        for gap_ms, start, expected in [
            (100, -.100, None),
            (120, 0., 'continuous_missing_data_reaches_hard_gap'),
            (2, -.110, 'recording_edge_reaches_hard_gap'),
        ]:
            with self.subTest(gap_ms=gap_ms, start=start):
                ticks = np.arange(20_000, dtype=np.int64)
                ticks = ticks[~((ticks > 10_000) & (ticks < 10_000+gap_ms) & (ticks % 2 == 0))]
                raw = np.zeros((len(ticks), 29), dtype=np.uint8)
                raw[:, :3] = 0xd2
                raw[:, 3] = np.where(ticks % 2 == 0, 0xaa, 0xbb)
                raw[:, 4] = ticks % 256
                args = (raw, ticks*1_000_000, {'t0': start, 't1': 19.999}, rebuild)
                if expected:
                    with self.assertRaisesRegex(ValueError, expected):
                        plan(*args)
                else:
                    self.assertEqual(plan(*args)[0]['hard_gap_s'], .1)


if __name__ == '__main__':
    unittest.main()
