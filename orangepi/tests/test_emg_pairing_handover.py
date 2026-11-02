"""Fresh calibration evidence survives reader shutdown, never USB changes."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import project_paths
import display_demand
import emg_insertion as insertion
import emg_insertion_controls as controls


def ready_pair():
    pair = insertion.OrderedPairing('boot')
    pair.observe('boot', [], [], [], 10, now_ns=100_000_000_000, wall_ns=1_000_000_000_000)
    pair.observe('boot', [], [], [], 10, now_ns=100_400_000_000, wall_ns=1_000_400_000_000)
    units = [dict(serial='left'), dict(serial='right')]
    stamp = [['/dev/a', '1a86', '55d3', 'left', 1_001_000_000_000],
             ['/dev/b', '1a86', '55d3', 'right', 1_002_000_000_000]]
    def event(serial, sequence, kernel):
        return dict(action='add', serial=serial, sequence=sequence,
            kernel_time_ns=kernel, received_ns=kernel+10_000_000,
            devpath='/devices/usb/'+serial, source='kernel_udevadm', vendor='1a86', product='55d3')
    pair.observe('boot', units[:1], stamp[:1], [event('left', 11, 101_000_000_000)], 11,
        now_ns=101_100_000_000, wall_ns=1_001_100_000_000)
    pair.observe('boot', units, stamp, [event('right', 12, 102_000_000_000)], 12,
        now_ns=102_100_000_000, wall_ns=1_002_100_000_000)
    sources = dict(zip(insertion.SLOTS, ('/dev/a', '/dev/b')))
    signals = {slot: dict(time=1002.1, count=250, rows=[[1002.1, [[-1, 1]]*8]],
        preview_id='live', source_path=path) for slot, path in sources.items()}
    return pair, units, stamp, sources, signals


class HandoverTests(unittest.TestCase):
    def exercise(self, delay=0, changed=False, stale=False, reboot=False):
        pair, units, stamp, sources, signals = ready_pair()
        self.assertTrue(pair.preview_ready, pair.snapshot())
        c = controls.EmgInsertionControls()
        c.worker = None
        c._emg_pairing = pair
        c._emg_pairing_config = {}
        c._emg_pairing_monitor = Mock()
        c._emg_pairing_monitor.drain.return_value = []
        c._emg_pairing_monitor.last_sequence = 12
        c.preview_worker = Mock()
        c.preview_worker.poll.return_value = None
        c.preview_snapshot_id = 'live'
        c.emg_preview_sources = sources
        if stale:
            signals['emg_right']['time'] -= 10
        c._read_pairing_signals = Mock(return_value=signals)
        c._finish_ordered_emg = Mock()
        c.state_write = Mock()
        c.close_emg_pairing = Mock()
        c._say_emg_pairing_phase = Mock()
        c.voice = Mock()
        clock = [1002.2]
        def stop():
            clock[0] += delay
        c.stop_preview = Mock(side_effect=stop)
        changed_stamp = copy.deepcopy(stamp)
        changed_stamp[0][4] += 1
        with patch.object(insertion.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(controls.emg_calibration, 'boot_id', side_effect=['boot', 'new-boot'] if reboot else lambda: 'boot'), \
             patch.object(controls.emg_calibration, 'current_stamp', side_effect=[stamp, changed_stamp] if changed else lambda config: stamp), \
             patch.object(insertion, 'usb_units', return_value=units), \
             patch.object(controls.capture_settings, 'capture_guard', side_effect=lambda base: nullcontext()):
            c.poll_emg_pairing()
        return c, pair, stamp

    def test_slow_preview_shutdown_keeps_fresh_accepted_proof(self):
        c, pair, stamp = self.exercise(delay=6.1)
        c._finish_ordered_emg.assert_called_once()
        proof, evidence = c._finish_ordered_emg.call_args[0]
        self.assertEqual(proof['completed_at'], 1002.2)
        self.assertTrue(insertion.valid(proof, 'boot', stamp))
        self.assertEqual(proof['preview'], evidence)

    def test_replug_during_shutdown_still_rejects(self):
        c, pair, _ = self.exercise(delay=6.1, changed=True)
        c._finish_ordered_emg.assert_not_called()
        self.assertEqual(pair.phase, 'error')

    def test_reboot_during_shutdown_still_rejects(self):
        c, pair, _ = self.exercise(delay=6.1, reboot=True)
        c._finish_ordered_emg.assert_not_called()
        self.assertEqual(pair.phase, 'error')

    def test_old_signal_is_never_accepted_before_shutdown(self):
        c, pair, _ = self.exercise(stale=True)
        c._finish_ordered_emg.assert_not_called()
        c.stop_preview.assert_not_called()
        self.assertFalse(pair.data_ready['emg_right'])


class CalibrationDemandTests(unittest.TestCase):
    def test_both_emg_sources_stay_live_without_a_visible_waveform(self):
        for state in ('emg_pairing', 'emg_manual'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                demand = display_demand.DisplayDemand(tmp)
                demand.state(dict(state=state))
                demand.touch('phone', (), False)
                reader = display_demand.DemandReader(tmp)
                reader.refresh()
                self.assertEqual(reader.signals, frozenset(insertion.SLOTS))
                self.assertFalse(reader.cameras)
                demand.state(dict(state='idle'))
                self.assertEqual(json.loads(demand.path.read_text())['signals'], [])

    def test_recording_and_privacy_block_forced_calibration_envelopes(self):
        for state in ('recording', 'countdown', 'emg_pairing'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                demand = display_demand.DisplayDemand(tmp)
                demand.state(dict(state='emg_pairing'))
                demand.state(dict(state=state, display_blocked=True))
                demand.touch('phone', insertion.SLOTS, True)
                value = json.loads(demand.path.read_text())
                self.assertEqual(value['signals'], [])
                self.assertFalse(value['cameras'])


if __name__ == '__main__':
    unittest.main()
