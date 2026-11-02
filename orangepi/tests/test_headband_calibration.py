"""Pure software tests; no camera, ROS node, or recording is started."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import headband_calibration as hc


class HeadbandCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.output = self.root / 'session'
        self.raw = {}
        cameras = {}
        # Deliberately put bleft before bright in the recording configuration.
        for slot in ('left', 'right', 'bleft', 'bright'):
            cameras[slot] = dict(topic='/fisheye/' + slot + '/image_raw/compressed',
                                 width=1920, height=1200)
        self.cfg = dict(cameras=cameras, headband_calibration=dict(enabled=True,
            source_dir=str(self.source), source_verified=True,
            device_id_file=str(self.root / 'DEVICE_ID'),
            hardware_model_file=str(self.root / 'HARDWARE_MODEL')))
        (self.root / 'DEVICE_ID').write_bytes(b'cp_0013\n')
        (self.root / 'HARDWARE_MODEL').write_bytes(b'baai-wuyu-ego4-0151-152\n')
        chain = {'cam' + str(i): dict(rostopic=cameras[slot]['topic'],
                                     resolution=[1920, 1200],
                                     intrinsics=[500, 501, 960, 600])
                 for i, slot in enumerate(('left', 'right', 'bright', 'bleft'))}
        for name, value in zip(hc.FILENAMES, (chain, chain, {'imu0': {'rostopic': '/imu_data_raw'}})):
            self.write_source(name, value)

    def write_source(self, name, value):
        # JSON is valid YAML; surrounding whitespace tests raw-byte preservation.
        self.raw[name] = b' \r\n' + json.dumps(value, indent=2).encode() + b'\r\n'
        (self.source / name).write_bytes(self.raw[name])

    def snapshot(self, cfg=None, output=None):
        return hc.snapshot(output or self.output, cfg or self.cfg)

    def test_verified_raw_bytes_manifest_identity_and_mapping(self):
        result = self.snapshot()
        self.assertEqual(result['state'], 'verified')
        self.assertIs(result['available'], True)
        self.assertIs(result['applicability_verified'], True)
        self.assertFalse(result['conflict'])
        target = self.output / result['snapshot_relative_dir']
        for name in hc.FILENAMES:
            self.assertEqual((target / name).read_bytes(), self.raw[name])
        entries = [dict(name=name, size=len(self.raw[name]), sha256=hashlib.sha256(self.raw[name]).hexdigest())
                   for name in hc.FILENAMES]
        self.assertEqual(json.loads((target / 'manifest.json').read_text()), dict(version=1, files=entries))
        self.assertEqual(result['files'], entries)
        self.assertEqual(result['device']['device_id']['value'], 'cp_0013')
        self.assertEqual(result['device']['hardware_model']['value'], 'baai-wuyu-ego4-0151-152')
        for mapping in result['cameras'].values():
            self.assertEqual(mapping['cam2']['slot'], 'bright')
            self.assertEqual(mapping['cam2']['label'], '后右相机')
            self.assertEqual(mapping['cam3']['slot'], 'bleft')
            self.assertEqual(mapping['cam3']['label'], '后左相机')
        self.assertEqual(json.loads((self.output/'ego_headband/calibration_status.json').read_text()), result)
        hc.verify_saved(self.output, result)

    def test_unverified_source_is_preserved_only_as_candidate(self):
        cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['source_verified'] = False
        result = self.snapshot(cfg)
        self.assertEqual(result['state'], 'candidate')
        self.assertIs(result['applicability_verified'], False)
        self.assertIn('source_not_verified', result['reasons'])
        self.assertFalse((self.output/'ego_headband/calibrations').exists())
        self.assertEqual((self.output/'ego_headband/calibration_candidates'/hc.FILENAMES[0]).read_bytes(), self.raw[hc.FILENAMES[0]])
        hc.verify_saved(self.output, result)

    def test_production_compressed_transport_matches_raw_calibration_topics(self):
        for name in hc.FILENAMES[:2]:
            value = json.loads(self.raw[name])
            for camera in value.values():
                camera['rostopic'] = camera['rostopic'][:-len('/compressed')]
            self.write_source(name, value)
        result = self.snapshot()
        self.assertEqual(result['state'], 'verified')
        self.assertTrue(result['applicability_verified'])
        mapping = result['cameras'][hc.FILENAMES[0]]
        self.assertEqual(mapping['cam2']['topic'], '/fisheye/bright/image_raw')
        self.assertEqual(mapping['cam2']['configured_topic'], '/fisheye/bright/image_raw/compressed')
        self.assertEqual(mapping['cam2']['slot'], 'bright')
        for name in hc.FILENAMES:
            self.assertEqual((self.output/result['snapshot_relative_dir']/name).read_bytes(), self.raw[name])

    def test_source_verified_requires_actual_boolean_true(self):
        for index, value in enumerate((False, 1, 'true', None, [], {})):
            with self.subTest(value=value):
                cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['source_verified'] = value
                result = self.snapshot(cfg, self.root/('flag-' + str(index)))
                self.assertEqual(result['state'], 'candidate')
                self.assertFalse(result['applicability_verified'])

    def test_resolution_mismatch_preserves_original_values_and_excludes_canonical(self):
        for name in hc.FILENAMES[:2]:
            value = json.loads(self.raw[name])
            for camera in value.values(): camera['resolution'] = [1920, 1088]
            self.write_source(name, value)
        result = self.snapshot()
        self.assertEqual(result['state'], 'candidate')
        self.assertTrue(any(reason.startswith('camera_resolution_mismatch:') for reason in result['reasons']))
        self.assertFalse((self.output/'ego_headband/calibrations').exists())
        for name in hc.FILENAMES:
            self.assertEqual((self.output/result['snapshot_relative_dir']/name).read_bytes(), self.raw[name])

    def test_unknown_duplicate_or_changed_topic_cannot_be_certified(self):
        for index, topic in enumerate(('/unknown', self.cfg['cameras']['left']['topic'])):
            with self.subTest(topic=topic):
                value = json.loads(self.raw[hc.FILENAMES[1]])
                value['cam2']['rostopic'] = topic
                self.write_source(hc.FILENAMES[1], value)
                result = self.snapshot(output=self.root/('topic-' + str(index)))
                self.assertEqual(result['state'], 'candidate')
                self.assertTrue(any('topic_mismatch' in reason for reason in result['reasons']))

    def test_missing_expected_dimensions_or_identity_is_unverified(self):
        cfg = copy.deepcopy(self.cfg); del cfg['cameras']['right']['height']
        result = self.snapshot(cfg)
        self.assertEqual(result['state'], 'candidate')
        self.assertIn('expected_camera_configuration_incomplete', result['reasons'])
        (self.root/'DEVICE_ID').unlink()
        result = self.snapshot(output=self.root/'missing-id')
        self.assertEqual(result['state'], 'candidate')
        self.assertIn('device_id_not_available', result['reasons'])

    def test_missing_empty_oversize_or_corrupt_source_returns_nonfatal_status(self):
        name = hc.FILENAMES[1]
        for index, value in enumerate((None, b'', b'x'*(hc.MAX_FILE_BYTES+1), b'cam0: [broken', b'[]')):
            with self.subTest(index=index):
                path = self.source/name
                if path.exists(): path.unlink()
                if value is not None: path.write_bytes(value)
                output = self.root/('bad-' + str(index))
                result = self.snapshot(output=output)
                self.assertEqual(result['state'], 'not_available')
                self.assertFalse(result['available'])
                self.assertTrue(result['warnings'])
                self.assertFalse((output/'ego_headband/calibrations').exists())
                self.assertFalse((output/'ego_headband/calibration_candidates').exists())
                self.assertEqual(json.loads((output/'ego_headband/calibration_status.json').read_text()), result)
                hc.verify_saved(output, result)

    def test_symlink_source_file_and_source_directory_are_rejected(self):
        name = hc.FILENAMES[0]; original = self.source/name
        real = self.root/'actual.yaml'; real.write_bytes(original.read_bytes())
        original.unlink(); original.symlink_to(real)
        self.assertEqual(self.snapshot()['state'], 'not_available')
        original.unlink(); original.write_bytes(self.raw[name])
        link = self.root/'linked-source'; link.symlink_to(self.source, target_is_directory=True)
        cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['source_dir'] = str(link)
        self.assertEqual(self.snapshot(cfg, self.root/'link-dir')['state'], 'not_available')

    def test_existing_same_snapshot_is_idempotent_but_different_bytes_never_overwrite(self):
        first = self.snapshot(); target = self.output/first['snapshot_relative_dir']
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        self.assertEqual(self.snapshot()['state'], 'verified')
        value = json.loads(self.raw[hc.FILENAMES[0]])
        value['cam0']['intrinsics'][0] += 1
        self.write_source(hc.FILENAMES[0], value)
        result = self.snapshot()
        self.assertEqual(result['state'], 'not_available')
        self.assertTrue(result['conflict'])
        self.assertIn('新会话', result['message'])
        self.assertNotIn('可继续', result['message'])
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)

    def test_existing_candidate_cannot_be_silently_promoted(self):
        cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['source_verified'] = False
        self.assertEqual(self.snapshot(cfg)['state'], 'candidate')
        result = self.snapshot()
        self.assertTrue(result['conflict'])
        self.assertFalse((self.output/'ego_headband/calibrations').exists())

    def test_existing_snapshot_identity_is_frozen_even_when_yaml_bytes_match(self):
        first = self.snapshot(); target = self.output/first['snapshot_relative_dir']
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        (self.root/'DEVICE_ID').write_text('another-body\n')
        result = self.snapshot()
        self.assertTrue(result['conflict'])
        self.assertEqual(result['state'], 'not_available')
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)

    def test_non_imu_file_is_unavailable_and_imu_topic_mismatch_is_candidate(self):
        self.write_source(hc.FILENAMES[2], {'unrelated': {'key': 'value'}})
        self.assertEqual(self.snapshot()['state'], 'not_available')
        self.write_source(hc.FILENAMES[2], {'imu0': {'rostopic': '/another_imu'}})
        cfg = copy.deepcopy(self.cfg); cfg['board_imu'] = {'topic': '/imu_data_raw'}
        result = self.snapshot(cfg, self.root/'imu-mismatch')
        self.assertEqual(result['state'], 'candidate')
        self.assertIn('imu_topic_mismatch', result['reasons'])

    def test_source_changed_between_reads_is_not_published(self):
        original = hc._read_file; changed = [False]
        def changing(path, limit=hc.MAX_FILE_BYTES):
            value = original(path, limit)
            if Path(path) == self.source/hc.FILENAMES[-1] and not changed[0]:
                changed[0] = True
                (self.source/hc.FILENAMES[0]).write_bytes(self.raw[hc.FILENAMES[0]] + b' ')
            return value
        with patch.object(hc, '_read_file', side_effect=changing): result = self.snapshot()
        self.assertEqual(result['state'], 'not_available')
        self.assertTrue(any('source_changed' in reason for reason in result['reasons']))
        self.assertFalse((self.output/'ego_headband/calibrations').exists())

    def test_source_changed_during_staging_is_not_published(self):
        original = hc._write_file; changed = [False]
        def changing(path, raw):
            original(path, raw)
            if not changed[0]:
                changed[0] = True
                (self.source/hc.FILENAMES[0]).write_bytes(self.raw[hc.FILENAMES[0]] + b' ')
        with patch.object(hc, '_write_file', side_effect=changing): result = self.snapshot()
        self.assertEqual(result['state'], 'not_available')
        self.assertFalse((self.output/'ego_headband/calibrations').exists())
        self.assertFalse(list((self.output/'ego_headband').glob('.headband-calibration-*')))

    def test_partial_write_never_publishes_incomplete_directory(self):
        original = hc._write_file; calls = [0]
        def failing(path, raw):
            calls[0] += 1
            if calls[0] == 2: raise OSError('simulated full storage')
            original(path, raw)
        with patch.object(hc, '_write_file', side_effect=failing): result = self.snapshot()
        self.assertEqual(result['state'], 'not_available')
        self.assertFalse((self.output/'ego_headband/calibrations').exists())
        self.assertFalse(list((self.output/'ego_headband').glob('.headband-calibration-*')))

    def test_verify_saved_detects_tamper_and_missing_for_both_modes(self):
        for verified in (True, False):
            cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['source_verified'] = verified
            output = self.root/('verify-' + str(verified))
            result = self.snapshot(cfg, output); target = output/result['snapshot_relative_dir']
            path = target/hc.FILENAMES[0]; path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaises(hc.SnapshotError): hc.verify_saved(output, result)
            path.write_bytes(self.raw[hc.FILENAMES[0]])
            manifest = target/'manifest.json'; manifest.write_bytes(manifest.read_bytes() + b' ')
            with self.assertRaises(hc.SnapshotError): hc.verify_saved(output, result)
            manifest.unlink()
            with self.assertRaises(hc.SnapshotError): hc.verify_saved(output, result)

    def test_symlink_destination_does_not_write_outside_session(self):
        self.output.mkdir(); external = self.root/'external'; external.mkdir()
        (self.output/'ego_headband').symlink_to(external, target_is_directory=True)
        self.assertEqual(self.snapshot()['state'], 'not_available')
        self.assertEqual(list(external.iterdir()), [])

    def test_disabled_does_not_touch_files(self):
        cfg = copy.deepcopy(self.cfg); cfg['headband_calibration']['enabled'] = False
        result = self.snapshot(cfg)
        self.assertEqual(result['state'], 'disabled')
        self.assertFalse(self.output.exists())
        hc.verify_saved(self.output, result)


if __name__ == '__main__':
    unittest.main()
