"""Advisory impedance keeps true measurements while allowing reviewed START."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard
import eeg_impedance
import impedance_quality as quality
import impedance_record as record
from impedance_estimator import ALGORITHM, SDK_REFERENCE
from eeg_settings import LAYOUT, LEGACY_LAYOUT, DEFAULT, validate


def config(policy='advisory'):
    return dict(impedance_acceptance_policy=policy, port='/dev/test-eeg', serial='test-serial',
        labels=list(LAYOUT), disabled_channels=[], sample_rate_hz=250,
        filter_preset='general', chunk_seconds=2)


def fixture(number=270., policy='advisory', reviewed=True):
    channels=[dict(channel=i+1,label=LAYOUT[i],enabled=True,
        value_kohm=number,reason='ok',accepted_samples=1000) for i in range(8)]
    frontend=dict(verified=True,enabled_mask=255,mode='eeg',reference='srb1')
    state=dict(required=True,supported=True,impedance_acceptance_policy=policy,
        phase='completed',active=False,pending=False,error=None,reviewed=reviewed,can_record=reviewed,
        measurement_epoch='epoch',measurement_started_at=100.,completed_at=101.,reviewed_at=102.,
        measurement_started_monotonic_ns=100_000_000_000,completed_monotonic_ns=101_000_000_000,
        reviewed_monotonic_ns=102_000_000_000,channels=channels,algorithm=ALGORITHM,
        sdk_reference=copy.deepcopy(SDK_REFERENCE),series_kohm=9.98,input_side='srb1',
        expected_channel_mask=255,window_ready=True,window_status='ready',sample_rate_hz=250,
        window_seconds=4,window_required_positions=1000,window_positions=1000,
        window_start_position=0,window_end_position=999,window_observed_span=1000,
        window_received_samples=1000,measurement_generation=1,normal_frame_generation=2,
        normal_frame_monotonic_ns=100_500_000_000,normal_frame_received_at=100.5,
        normal_recovery_samples_consumed=1,loff_off_confirmed=True,frontend_restored=True,
        normal_frame_confirmed=True,config_fingerprint=record.fingerprint(frontend),stale=False)
    state['quality']=quality.assess_impedance(state)
    return state,frontend


class AdvisoryTests(unittest.TestCase):
    def test_default_profile_enables_all_eight_in_the_new_physical_order(self):
        value=validate(copy.deepcopy(DEFAULT))
        self.assertEqual(value['disabled_channels'],[])
        self.assertEqual(value['labels'],['FC3','C3','O1','Pz','Oz','O2','C4','FC4'])
        wrong=copy.deepcopy(value);wrong['disabled_channels']=[1]
        with self.assertRaises(ValueError):validate(wrong)

    def test_old_impedance_evidence_cannot_authorize_new_eight_channel_start(self):
        state,frontend=fixture();frontend['enabled_mask']=254
        state['expected_channel_mask']=254
        for i,row in enumerate(state['channels']):row['label']=LEGACY_LAYOUT[i]
        state['channels'][0].update(enabled=False,value_kohm=None,reason='disabled',accepted_samples=0)
        old=config();old.update(labels=list(LEGACY_LAYOUT),disabled_channels=[1])
        with tempfile.TemporaryDirectory() as root:
            persisted=self.save(root,state,frontend)
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,persisted,config())
            gate=dict(start_monotonic_ns=103_000_000_000,start_unix_ns=103_000_000_000)
            gate['eeg_impedance']=record.bind_start(root,gate,persisted,old)
            record.atomic_record(Path(root)/'start_gate.json',gate)
            self.assertEqual(record.validate_start_binding(root,gate,old)['impedance_acceptance_policy'],'advisory')

    def test_numeric_result_remains_truthful_at_and_above_60(self):
        for value in (0.,59.9,60.,270.,1000.):
            state,_=fixture(value)
            self.assertEqual(quality.assess_impedance(state)['passed'],value<60)
            self.assertTrue(dashboard.impedance_allows_recording(state))
            self.assertIn('start',dashboard.actions_for('ready',state))

    def test_strict_mode_retains_its_existing_threshold(self):
        for value in (59.9,60.,270.):
            state,_=fixture(value,'strict')
            self.assertEqual(dashboard.impedance_allows_recording(state),value<60)

    def test_active_pending_unrestored_or_unreviewed_state_cannot_start(self):
        for changes in (dict(active=True),dict(pending=True),dict(error='restore failed'),
                        dict(frontend_restored=False),dict(loff_off_confirmed=False),dict(can_record=False),
                        dict(phase='fault'),dict(normal_frame_confirmed=False),dict(reviewed=False),dict(window_ready=False)):
            state,_=fixture();state.update(changes)
            self.assertFalse(dashboard.impedance_allows_recording(state))

    def owner(self,state):
        owner=eeg_impedance.Preflight.__new__(eeg_impedance.Preflight)
        owner.state=state;owner.required=True;owner.mask=255;owner.emit=Mock();owner._save=Mock()
        return owner

    def test_owner_review_accepts_high_values_without_falsifying_quality(self):
        state,_=fixture(reviewed=False);owner=self.owner(state)
        owner.handle(dict(command='IMPEDANCE_REVIEW'))
        self.assertTrue(owner.state['can_record']);self.assertTrue(owner.state['reviewed'])
        self.assertFalse(owner.state['quality']['passed']);self.assertEqual(owner.state['channels'][1]['value_kohm'],270.)
        owner.publish();self.assertTrue(owner.state['can_record'])

    def test_strict_owner_review_still_rejects_high_values(self):
        state,_=fixture(policy='strict',reviewed=False);owner=self.owner(state)
        with self.assertRaises(RuntimeError):owner.handle(dict(command='IMPEDANCE_REVIEW'))
        self.assertFalse(owner.state['can_record'])

    def test_owner_review_still_requires_completed_normal_recovery(self):
        for key,value in (('normal_frame_confirmed',False),('phase','measuring'),('window_ready',False)):
            state,_=fixture(reviewed=False);state[key]=value
            with self.assertRaises(RuntimeError):self.owner(state).handle(dict(command='IMPEDANCE_REVIEW'))

    def save(self,root,state,frontend):
        return record.save_preflight(root,state,frontend,dict(port='/dev/test-eeg',configured_usb_serial='test-serial'))

    def bind(self,root,state,frontend,policy='advisory'):
        persisted=self.save(root,state,frontend)
        gate=dict(start_monotonic_ns=103_000_000_000,start_unix_ns=103_000_000_000)
        gate['eeg_impedance']=record.bind_start(root,gate,persisted,config(policy))
        record.atomic_record(Path(root)/'start_gate.json',gate)
        return persisted,gate

    def test_high_result_survives_start_binding_and_historical_qc_validation(self):
        for value in (59.9,60.,270.,1000.):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as root:
                state,frontend=fixture(value);persisted,gate=self.bind(root,state,frontend)
                proof=record.validate_start_binding(root,gate,config())
                self.assertEqual(proof['impedance_acceptance_policy'],'advisory')
                self.assertEqual(persisted['quality']['passed'],value<60)
                self.assertEqual(persisted['channels'][1]['value_kohm'],value)

    def test_failed_or_missing_readings_are_retained_as_advisory(self):
        state,frontend=fixture()
        for row in state['channels']:row.update(value_kohm=None,reason='adc_saturated',accepted_samples=0)
        with tempfile.TemporaryDirectory() as root:
            persisted,gate=self.bind(root,state,frontend)
            self.assertFalse(persisted['quality']['passed']);self.assertEqual(persisted['valid_channel_count'],0)
            self.assertEqual(record.validate_start_binding(root,gate,config())['impedance_acceptance_policy'],'advisory')

    def test_record_cannot_silently_change_the_configured_policy(self):
        state,frontend=fixture()
        with tempfile.TemporaryDirectory() as root:
            persisted=self.save(root,state,frontend)
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,persisted,config('strict'))
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root)

    def test_quality_tampering_is_rejected_even_in_advisory_mode(self):
        state,frontend=fixture()
        with tempfile.TemporaryDirectory() as root:
            persisted=self.save(root,state,frontend);persisted['quality']['passed']=True
            record.atomic_record(Path(root)/record.RELATIVE_PATH,persisted)
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,persisted,config())

    def test_unrestored_or_wrong_device_evidence_is_rejected(self):
        for changes in (dict(frontend_restored=False),dict(normal_frame_confirmed=False)):
            state,frontend=fixture();state.update(changes)
            with tempfile.TemporaryDirectory() as root:
                persisted=self.save(root,state,frontend)
                with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,persisted,config())
        state,frontend=fixture()
        with tempfile.TemporaryDirectory() as root:
            persisted=self.save(root,state,frontend);wrong=config();wrong['serial']='other'
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,persisted,wrong)

    def test_unknown_policy_is_rejected_and_default_is_strict(self):
        self.assertEqual(quality.acceptance_policy({}),'strict')
        with self.assertRaises(ValueError):validate(config('typo'))


if __name__=='__main__':unittest.main()
