"""Astra ae60c5c R1/R2 reproductions and positive boundary regressions."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'orangepi/src/orangepi_collect/control'))
from snapshot_contract import aged, normalize, validate
from snapshot_publisher import SnapshotPublisher


class ReviewContractTests(unittest.TestCase):
    def setUp(self):
        self.value = json.loads((ROOT/'contracts/v1/fixtures/device.json').read_text())

    def test_r1_same_boot_age_cannot_underestimate_device_elapsed(self):
        self.value['resources']['ram_sample'].update(sampled_mono_ms=0, age_ms=0)
        with self.assertRaises(ValueError): validate(self.value)

    def test_r1_host_elapsed_and_response_age_are_lower_bound_not_equality(self):
        validate(self.value)
        projected = aged(self.value, 3000)  # Response age + host elapsed.
        validate(projected)
        self.assertEqual(projected['time'], self.value['time'])
        self.assertEqual(projected['resources']['ram_sample']['age_ms'], 5000)
        validate(aged(self.value, 60000))  # Stale projection is still valid.
        clock = [100000]
        publisher = SnapshotPublisher(self.value['device_id'], self.value['boot_id'], clock=lambda:clock[0])
        evidence = copy.deepcopy(self.value)
        evidence['controller_instance'] = publisher.controller_instance
        publisher.publish(evidence)
        clock[0] += 20000
        item = publisher.publish(evidence)
        value = json.loads(item.body)
        validate(value)
        self.assertEqual(value['resources']['ram_sample']['sampled_mono_ms'], 98000)
        self.assertEqual(value['resources']['ram_sample']['age_ms'], 22000)
        self.assertEqual(value['resources']['ram_sample']['quality'], 'stale')

    def test_r2_null_is_not_calibration_proof(self):
        value = normalize('cp_0015')
        value['roles']['left'].update(confirmed=True, calibration_valid=True)
        with self.assertRaises(ValueError): validate(value)
        for field in ('binding_id', 'calibration_version', 'profile_epoch', 'proof_boot_id'):
            with self.subTest(field=field):
                value = copy.deepcopy(self.value)
                value['roles']['left'][field] = None
                if field == 'profile_epoch':
                    value['session']['profile_epoch'] = None
                    value['roles']['right']['calibration_valid'] = None
                    value['progress']['total'] = None
                with self.assertRaises(ValueError): validate(value)

    def test_r2_null_epoch_is_not_review_identity(self):
        value = copy.deepcopy(self.value)
        value['session']['profile_epoch'] = None
        value['progress']['total'] = None
        for side in ('left', 'right'):
            value['roles'][side].update(profile_epoch=None, binding_id=None,
                                        calibration_version=None, calibration_valid=None)
        value['health']['impedance'].update(measurement_id='m1', owner_run='o1',
            session_id=value['session']['session_id'], profile_epoch=None,
            quality_version='q1', quality='pass', can_review=True)
        with self.assertRaises(ValueError): validate(value)

    def test_r2_complete_proofs_survive_observation_stale_not_controller_reset(self):
        self.value['health']['impedance'].update(measurement_id='m1', owner_run='o1',
            session_id=self.value['session']['session_id'], profile_epoch=3,
            quality_version='q1', quality='pass', can_review=True)
        validate(self.value)
        old = aged(self.value, 60000)
        validate(old)
        self.assertTrue(old['health']['impedance']['can_review'])
        self.assertTrue(old['roles']['left']['calibration_valid'])
        publisher = SnapshotPublisher(self.value['device_id'], self.value['boot_id'], self.value['controller_instance'], clock=lambda:100000)
        publisher.publish(self.value, origin_instance=publisher.controller_instance)
        publisher.reset(publisher.device_id, publisher.boot_id)
        self.assertIsNone(publisher.read())
        with self.assertRaises(ValueError):
            publisher.publish(self.value, origin_instance=self.value['controller_instance'])
        current = json.loads(publisher.publish().body)
        self.assertIsNone(current['health']['impedance']['can_review'])
        self.assertIsNone(current['roles']['left']['calibration_valid'])


if __name__ == '__main__': unittest.main()
