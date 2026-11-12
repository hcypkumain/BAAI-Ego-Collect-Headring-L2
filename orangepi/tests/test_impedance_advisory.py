"""Advisory impedance keeps true measurements while allowing reviewed START."""
import copy
from dataclasses import make_dataclass
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        measurement_epoch='epoch',owner_id='owner',saved_owner_id='owner',saved_measurement_epoch='epoch',
        completed_persisted=True,saved_config_fingerprint=record.fingerprint(frontend),measurement_started_at=100.,completed_at=101.,reviewed_at=102.,
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

    def test_real_profile_loader_default_and_explicit_policy(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            base=Path(root); (base/'configs').mkdir()
            with patch.object(project_paths,'BASE',base), patch.object(project_paths,'COLLECTION_MODE','eeg'), \
                 patch('device_discovery.discover',side_effect=lambda v,b:v), \
                 patch('wrist_calibration.apply',side_effect=lambda v,b:v), \
                 patch('emg_calibration.apply',side_effect=lambda v,b:v), \
                 patch('wrist_camera_settings.apply',side_effect=lambda v,b:v), \
                 patch.dict(os.environ,{},clear=True):
                for text,expected in (('eeg: {}\n','advisory'),('eeg:\n  impedance_acceptance_policy: strict\n','strict')):
                    (base/'configs/devices.yaml').write_text(text)
                    self.assertEqual(project_paths.devices()['eeg']['impedance_acceptance_policy'],expected)

    def test_review_persist_failure_revokes_gate(self):
        state,_=fixture(reviewed=False);owner=self.owner(state);owner._save.side_effect=RuntimeError('disk error')
        with self.assertRaises(RuntimeError):owner.handle(dict(command='IMPEDANCE_REVIEW',measurement_epoch='epoch'))
        self.assertFalse(owner.state['reviewed']);self.assertFalse(owner.state['can_record'])

    def test_old_impedance_evidence_cannot_authorize_new_eight_channel_start(self):
        state,frontend=fixture();frontend['enabled_mask']=254
        state['expected_channel_mask']=254
        state['config_fingerprint']=state['saved_config_fingerprint']=record.fingerprint(frontend)
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
        frontend=dict(verified=True,enabled_mask=255,mode='eeg',reference='srb1')
        Frontend=make_dataclass('Frontend',[(key,type(value)) for key,value in frontend.items()])
        owner.board=Mock();owner.board.get_config.return_value=Frontend(**frontend)
        return owner

    def test_owner_review_accepts_high_values_without_falsifying_quality(self):
        state,_=fixture(reviewed=False);owner=self.owner(state)
        owner.handle(dict(command='IMPEDANCE_REVIEW',measurement_epoch='epoch'))
        self.assertTrue(owner.state['can_record']);self.assertTrue(owner.state['reviewed'])
        self.assertFalse(owner.state['quality']['passed']);self.assertEqual(owner.state['channels'][1]['value_kohm'],270.)
        owner.publish();self.assertTrue(owner.state['can_record'])

    def test_strict_owner_review_still_rejects_high_values(self):
        state,_=fixture(policy='strict',reviewed=False);owner=self.owner(state)
        with self.assertRaises(RuntimeError):owner.handle(dict(command='IMPEDANCE_REVIEW',measurement_epoch='epoch'))
        self.assertFalse(owner.state['can_record'])

    def test_owner_review_still_requires_completed_normal_recovery(self):
        for key,value in (('normal_frame_confirmed',False),('phase','measuring'),('window_ready',False)):
            state,_=fixture(reviewed=False);state[key]=value
            with self.assertRaises(RuntimeError):self.owner(state).handle(dict(command='IMPEDANCE_REVIEW',measurement_epoch='epoch'))

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

    def test_missing_readings_never_authorize_new_start(self):
        for count in (1, 8):
            state,frontend=fixture()
            for row in state['channels'][:count]:row.update(value_kohm=None,reason='adc_saturated',accepted_samples=0)
            self.assertFalse(quality.quality_allows_recording(state))
            with tempfile.TemporaryDirectory() as root:
                persisted=self.save(root,state,frontend)
                with self.assertRaises(record.ImpedanceRecordError):
                    record.validate_preflight(root,persisted,config())
                historical=record._validate_preflight(root,persisted,config(),require_project_quality=False)
                self.assertEqual(historical['impedance_acceptance_policy'],'advisory')

    def test_qc_warns_high_and_errors_for_tampered_proof(self):
        from types import SimpleNamespace
        from eeg_qc import OmniEegImpedance
        with tempfile.TemporaryDirectory() as root:
            state,frontend=fixture(270.);persisted,gate=self.bind(root,state,frontend)
            eeg=Path(root)/'eeg'
            record.atomic_record(eeg/'metadata.json',dict(config=config(),frontend_config=frontend))
            check=OmniEegImpedance(); result=check.run(SimpleNamespace(dir=eeg))
            self.assertEqual(result.level,'WARN')
            self.assertFalse(result.stats['impedance_quality_passed'])
            self.assertEqual(result.stats['impedance_values_kohm'][LAYOUT[0]],270.)
            persisted['channels'][0]['value_kohm']=42.
            record.atomic_record(eeg/'impedance_preflight.json',persisted)
            self.assertEqual(check.run(SimpleNamespace(dir=eeg)).level,'ERROR')

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
