import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_impedance_advisory as advisory
from test_impedance_advisory import fixture
import impedance_quality as quality
import impedance_record as record


class ReviewEligibilityTests(unittest.TestCase):
    def test_completed_evidence_does_not_expire_with_wall_time(self):
        state,_=fixture(reviewed=False)
        state['updated_at']=1
        self.assertTrue(quality.review_eligibility(state)['can_review'])
        self.assertFalse(quality.assess_impedance(state)['passed'])

    def test_enabled_mask_and_invalid_values(self):
        state,frontend=fixture(reviewed=False)
        frontend['enabled_mask']=7;state['expected_channel_mask']=7
        state['config_fingerprint']=state['saved_config_fingerprint']=record.fingerprint(frontend)
        for row in state['channels'][3:]:row.update(enabled=False,value_kohm=None,reason='disabled',accepted_samples=0)
        self.assertEqual(quality.review_eligibility(state)['valid_count'],3)
        self.assertTrue(quality.review_eligibility(state)['can_review'])
        for bad in (None,-1,float('nan'),float('inf')):
            broken=copy.deepcopy(state);broken['channels'][0]['value_kohm']=bad
            self.assertIn('missing_valid_channels',quality.review_eligibility(broken)['reason_codes'])

    def test_durable_validation_accepts_exact_non_eight_channel_mask(self):
        state,frontend=fixture(reviewed=True);frontend['enabled_mask']=7;state['expected_channel_mask']=7
        state['config_fingerprint']=state['saved_config_fingerprint']=record.fingerprint(frontend)
        for row in state['channels'][3:]:row.update(enabled=False,value_kohm=None,reason='disabled',accepted_samples=0)
        cfg=advisory.config();cfg['disabled_channels']=[4,5,6,7,8]
        with tempfile.TemporaryDirectory() as root:
            saved=record.save_preflight(root,state,frontend,dict(port=cfg['port'],configured_usb_serial=cfg['serial']))
            record.validate_preflight(root,saved,cfg)
            cfg['disabled_channels']=[4,5,6,7]
            with self.assertRaises(record.ImpedanceRecordError):record.validate_preflight(root,saved,cfg)

    def test_each_safety_condition_denies_review(self):
        for changes,reason in (({'window_ready':False},'incomplete_window'),
            ({'normal_frame_confirmed':False},'normal_recovery_unconfirmed'),
            ({'completed_persisted':False},'persist_failed'),
            ({'owner_id':'other'},'owner_changed'),({'measurement_epoch':'new'},'measurement_changed'),
            ({'config_fingerprint':'other'},'configuration_changed'),({'pending':True},'measurement_not_completed')):
            with self.subTest(reason=reason):
                state,_=fixture(reviewed=False);state.update(changes)
                self.assertIn(reason,quality.review_eligibility(state)['reason_codes'])
        state,_=fixture(reviewed=False)
        self.assertIn('capture_armed',quality.review_eligibility(state,capture_armed=True)['reason_codes'])

    def test_wrong_epoch_or_changed_frontend_cannot_review(self):
        helper=advisory.AdvisoryTests();state,_=fixture(reviewed=False);owner=helper.owner(state)
        with self.assertRaisesRegex(RuntimeError,'测量已变化'):
            owner.handle({'command':'IMPEDANCE_REVIEW','measurement_epoch':'old'})
        self.assertFalse(owner.state['can_record'])
        owner.board.get_config.return_value.enabled_mask=7
        with self.assertRaisesRegex(RuntimeError,'配置已变化'):
            owner.handle({'command':'IMPEDANCE_REVIEW','measurement_epoch':'epoch'})
        self.assertFalse(owner.state['completed_persisted'])

    def test_completed_binding_invalidates_on_disconnect_generation_or_config_change(self):
        from types import SimpleNamespace
        helper=advisory.AdvisoryTests()
        for changed in ('generation','config'):
            state,_=fixture(reviewed=False);owner=helper.owner(state)
            if changed=='config':owner.board.get_config.return_value.enabled_mask=7
            owner.check_completed_binding(SimpleNamespace(generation=3 if changed=='generation' else 2))
            self.assertEqual(owner.state['phase'],'interrupted')
            self.assertFalse(owner.state['completed_persisted'])
            self.assertFalse(owner.state['can_record'])

    def test_completed_saved_marker_only_after_durable_save(self):
        helper=advisory.AdvisoryTests();state,_=fixture(reviewed=False);owner=helper.owner(state)
        owner._save=__import__('types').MethodType(__import__('eeg_impedance').Preflight._save,owner)
        owner.storage_error=None;owner.config={'port':'test','serial':'test'}
        state.update(completed_persisted=False,saved_owner_id=None,saved_measurement_epoch=None,saved_config_fingerprint=None)
        with tempfile.TemporaryDirectory() as root:
            owner.output=Path(root)
            owner._save('completed')
            self.assertTrue(owner.state['completed_persisted'])
            self.assertEqual(owner.state['saved_measurement_epoch'],'epoch')
            self.assertTrue(quality.review_eligibility(owner.state)['can_review'])
            with patch('eeg_impedance.save_preflight',side_effect=OSError('fsync uncertain')):
                with self.assertRaises(RuntimeError):owner._save('completed')
            self.assertFalse(owner.state['completed_persisted'])
            self.assertIn('persist_failed',quality.review_eligibility(owner.state)['reason_codes'])

    def test_directory_fsync_failure_after_replace_is_not_published_as_saved(self):
        helper=advisory.AdvisoryTests();state,_=fixture(reviewed=False);owner=helper.owner(state)
        owner._save=__import__('types').MethodType(__import__('eeg_impedance').Preflight._save,owner)
        owner.storage_error=None;owner.config={'port':'test','serial':'test'}
        state.update(completed_persisted=False,saved_owner_id=None,saved_measurement_epoch=None,saved_config_fingerprint=None)
        with tempfile.TemporaryDirectory() as root:
            owner.output=Path(root)
            with patch('impedance_record._sync_directory',side_effect=[None,OSError('directory fsync failed')]):
                with self.assertRaises(RuntimeError):owner._save('completed')
            self.assertTrue((Path(root)/record.RELATIVE_PATH).exists(),'replace happened before uncertain directory sync')
            self.assertFalse(owner.state['completed_persisted'])
            self.assertFalse(quality.review_eligibility(owner.state)['can_review'])


if __name__=='__main__':unittest.main()
