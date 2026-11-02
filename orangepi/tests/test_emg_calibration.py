"""Manual identity proofs never infer body sides from signal statistics."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import emg_calibration as ec

STAMP = [['/dev/a', '1a86', '55d3', 'new-a', 1],
         ['/dev/b', '1a86', '55d3', 'new-b', 2]]
SOURCES = dict(zip(ec.SLOTS, ('/dev/a', '/dev/b')))

def config():
    return {'emg_calibration': {'enabled': True}, 'emg': {
        slot: {'port': path, 'label': 'old side',
               'discovery': {'present': True, 'role_confirmed': True}}
        for slot, path in SOURCES.items()}}

def complete(left_slot='emg_right'):
    return ec.manual_proof('boot-a', STAMP, SOURCES, left_slot)

class EmgCalibrationTests(unittest.TestCase):
    def test_both_explicit_choices_map_exact_current_sources(self):
        for selected in ec.SLOTS:
            proof = complete(selected)
            self.assertEqual(proof['roles']['emg_left'], SOURCES[selected])
            self.assertEqual(set(proof['roles'].values()), set(SOURCES.values()))
            self.assertEqual(proof['sources'], SOURCES)
            self.assertEqual(proof['method'], 'manual_emg_preview')
            self.assertIs(proof['manual_confirmed'], True)
            self.assertTrue(ec.valid(proof, 'boot-a', STAMP))

    def test_no_automatic_statistics_path_can_confirm(self):
        wizard = ec.Wizard(config(), 'boot-a', STAMP)
        self.assertEqual(wizard.phase, 'manual')
        for stats in ({}, {'motion': 1000}, {'emg_frames': 0}, None):
            with self.assertRaises(ec.CalibrationError): wizard.accept(stats)
        with self.assertRaises(ec.CalibrationError): wizard.proof()
        self.assertEqual(wizard.phase, 'manual')

    def test_wizard_copies_identity_and_each_session_has_unique_token(self):
        cfg = config(); stamp = copy.deepcopy(STAMP)
        wizard = ec.Wizard(cfg, 'boot-a', stamp)
        cfg['emg']['emg_left']['port'] = '/dev/other'; stamp[0][-1] = 99
        self.assertEqual(wizard.sources, SOURCES)
        self.assertEqual(wizard.stamp, STAMP)
        self.assertNotEqual(wizard.session_id, ec.Wizard(config(), 'boot-a', STAMP).session_id)
        self.assertTrue(wizard.unchanged('boot-a', STAMP))
        self.assertFalse(wizard.unchanged('boot-b', STAMP))
        self.assertFalse(wizard.unchanged('boot-a', stamp))

    def test_same_boot_reuses_but_boot_replug_or_pair_change_invalidates(self):
        proof = complete()
        self.assertFalse(ec.valid(proof, 'boot-b', STAMP))
        for index, value in ((0, '/dev/new'), (1, 'abcd'), (2, '1234'), (3, 'new-c'), (4, 99)):
            stamp = copy.deepcopy(STAMP); stamp[0][index] = value
            self.assertFalse(ec.valid(proof, 'boot-a', stamp))
        self.assertFalse(ec.valid(proof, 'boot-a', STAMP[:1]))
        self.assertFalse(ec.valid(proof, 'boot-a', STAMP + [['/dev/c', '1a86', '55d3', 'c', 3]]))

    def test_incomplete_duplicate_or_malformed_pair_cannot_confirm(self):
        bad = [[], STAMP[:1], STAMP+[STAMP[0]], [STAMP[0], STAMP[0]], None]
        for field, value in ((0, ''), (1, ''), (2, None), (3, ''), (3, 'new-b'), (4, 0), (4, True), (4, 1.5)):
            stamp = copy.deepcopy(STAMP); stamp[0][field] = value; bad.append(stamp)
        for stamp in bad:
            with self.subTest(stamp=stamp):
                with self.assertRaises(ec.CalibrationError): ec.Wizard(config(), 'boot-a', stamp)
                with self.assertRaises(ec.CalibrationError): ec.manual_proof('boot-a', stamp, SOURCES, 'emg_left')
        for boot in ('', None):
            with self.assertRaises(ec.CalibrationError): ec.Wizard(config(), boot, STAMP)
        cfg = config(); cfg['emg']['emg_right']['discovery']['present'] = False
        with self.assertRaises(ec.CalibrationError): ec.Wizard(cfg, 'boot-a', STAMP)

    def test_invalid_selection_or_sources_cannot_confirm(self):
        for sources in ({}, None, {'emg_left': '/dev/a'},
                        {'emg_left': '/dev/a', 'emg_right': '/dev/a'},
                        {'emg_left': '/dev/a', 'emg_right': '/dev/c'},
                        {'emg_left': '/dev/a', 'emg_right': None}):
            with self.assertRaises(ec.CalibrationError): ec.manual_proof('boot-a', STAMP, sources, 'emg_left')
        for selected in (None, '', '/dev/a', 'A'):
            with self.assertRaises(ec.CalibrationError): ec.manual_proof('boot-a', STAMP, SOURCES, selected)

    def test_old_automatic_or_incomplete_proof_is_rejected(self):
        changes = [('version', 1), ('method', 'alternating_arm_motion'),
                   ('manual_confirmed', False), ('calibration_id', ''),
                   ('completed_at', float('nan')), ('completed_at', float('inf')),
                   ('completed_at', True), ('completed_at', 0),
                   ('roles', {}), ('sources', {}), ('boot_id', None)]
        for key, value in changes:
            proof = complete(); proof[key] = value
            self.assertFalse(ec.valid(proof, 'boot-a', STAMP), (key, value))
        for key in ('sources', 'manual_confirmed', 'method', 'calibration_id'):
            proof = complete(); del proof[key]
            self.assertFalse(ec.valid(proof, 'boot-a', STAMP))
        self.assertFalse(ec.valid(None, 'boot-a', STAMP))

    def test_old_confirmed_binding_is_pending_without_manual_proof(self):
        cfg = config(); original = copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as tmp, patch.object(ec, 'boot_id', return_value='boot-a'), patch.object(ec, 'current_stamp', return_value=STAMP):
            result = ec.apply(cfg, tmp)
        self.assertEqual(cfg, original)
        self.assertFalse(result['emg_calibration_status']['valid'])
        self.assertIsNone(result['emg_calibration_status']['proof'])
        for item in result['emg'].values():
            self.assertFalse(item['discovery']['role_confirmed'])
            self.assertIn('待人工确认左右', item['label'])

    def test_saved_proof_maps_sources_and_freezes_full_recording_evidence(self):
        cfg = config(); original = copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as tmp, patch.object(ec, 'boot_id', return_value='boot-a'), patch.object(ec, 'current_stamp', return_value=STAMP):
            proof = complete(); ec.save(tmp, proof)
            self.assertEqual(ec.load(tmp), proof)
            result = ec.apply(cfg, tmp); frozen = copy.deepcopy(result)
            self.assertEqual(result['emg']['emg_left']['port'], '/dev/b')
            self.assertEqual(result['emg']['emg_right']['port'], '/dev/a')
            self.assertEqual(result['emg_calibration_status']['proof'], proof)
            self.assertTrue(all(x['discovery']['role_confirmed'] for x in result['emg'].values()))
            ec.invalidate(tmp)
            self.assertFalse(ec.apply(cfg, tmp)['emg_calibration_status']['valid'])
            ec.save(tmp, complete('emg_left'))
            self.assertEqual(result, frozen)
            proof['roles']['emg_left'] = '/dev/changed'
            self.assertEqual(result, frozen)
        self.assertEqual(cfg, original)

    def test_corrupt_json_and_failed_atomic_save_do_not_replace_valid_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            ec.save(tmp, complete()); before = ec.load(tmp)
            with self.assertRaises(ValueError): ec.save(tmp, {'invalid': float('nan')})
            self.assertEqual(ec.load(tmp), before)
            self.assertEqual(list((Path(tmp)/'runtime').glob('.emg-calibration-*')), [])
            path = Path(tmp)/'runtime/emg_calibration.json'
            for text in ('{bad', '[]', 'null'):
                path.write_text(text)
                self.assertEqual(ec.load(tmp), {})
            ec.invalidate(tmp)

    def test_disabled_mode_passes_through_without_device_inspection(self):
        cfg = config(); cfg['emg_calibration']['enabled'] = False
        with patch.object(ec, 'boot_id', side_effect=AssertionError('no read')), patch.object(ec, 'current_stamp', side_effect=AssertionError('no inventory')), patch.object(ec, 'load', side_effect=AssertionError('no proof read')):
            self.assertIs(ec.apply(cfg, '/unused'), cfg)

if __name__ == '__main__': unittest.main()
