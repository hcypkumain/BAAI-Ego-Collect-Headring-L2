import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import wrist_calibration as wc


def sample(left=100, right=120):
    return {'/dev/cam-a': {'mean': left, 'dark': float(left < 20), 'frames': 24},
            '/dev/cam-b': {'mean': right, 'dark': float(right < 20), 'frames': 24}}


def config():
    return {'wrist_calibration': {'enabled': True}, 'wrist_cameras': {
        'cam_left_wrist': {'device': '/dev/cam-a', 'label': 'left', 'width': 640, 'height': 480, 'fps': 30,
                           'discovery': {'present': True, 'role_confirmed': True}},
        'cam_right_wrist': {'device': '/dev/cam-b', 'label': 'right', 'width': 640, 'height': 480, 'fps': 30,
                            'discovery': {'present': True, 'role_confirmed': True}}}}


class BootCalibrationTests(unittest.TestCase):
    def wizard(self):
        return wc.Wizard(config(), 'boot-a', [['/dev/cam-a', 1], ['/dev/cam-b', 2]])

    def finish(self, wizard):
        wizard.accept(sample())
        wizard.accept(sample(100, 1))  # Operator's left is source B.
        wizard.accept(sample(1, 120))
        wizard.accept(sample())
        return wizard.proof()

    def test_user_positions_override_device_order(self):
        proof = self.finish(self.wizard())
        self.assertEqual(proof['roles'], {'cam_left_wrist': '/dev/cam-b', 'cam_right_wrist': '/dev/cam-a'})

    def test_second_recording_and_controller_restart_reuse_same_boot(self):
        proof = self.finish(self.wizard())
        for _ in range(3):
            self.assertTrue(wc.valid(proof, 'boot-a', [['/dev/cam-a', 1], ['/dev/cam-b', 2]]))

    def test_new_boot_requires_calibration_even_with_same_ports(self):
        proof = self.finish(self.wizard())
        self.assertFalse(wc.valid(proof, 'boot-b', proof['stamp']))

    def test_replug_same_node_invalidates_old_result(self):
        proof = self.finish(self.wizard())
        self.assertFalse(wc.valid(proof, 'boot-a', [['/dev/cam-a', 3], ['/dev/cam-b', 2]]))

    def test_initial_dark_scene_is_rejected(self):
        w = self.wizard()
        with self.assertRaises(wc.CalibrationError): w.accept(sample(1, 1))
        self.assertEqual(w.phase, 'baseline')

    def test_both_or_neither_covered_cannot_assign_left(self):
        for values in [(1, 1), (100, 120)]:
            w = self.wizard(); w.accept(sample())
            with self.assertRaises(wc.CalibrationError): w.accept(sample(*values))
            self.assertEqual(w.phase, 'left')

    def test_same_camera_covered_twice_is_rejected(self):
        w = self.wizard(); w.accept(sample()); w.accept(sample(1, 120))
        with self.assertRaises(wc.CalibrationError): w.accept(sample(1, 120))
        self.assertEqual(w.phase, 'right')

    def test_finish_requires_both_uncovered(self):
        w = self.wizard(); w.accept(sample()); w.accept(sample(1, 120)); w.accept(sample(100, 1))
        with self.assertRaises(wc.CalibrationError): w.accept(sample(100, 1))
        with self.assertRaises(wc.CalibrationError): w.proof()

    def test_missing_stream_or_few_frames_is_rejected(self):
        w = self.wizard(); bad = sample(); bad['/dev/cam-a']['frames'] = 2
        with self.assertRaises(wc.CalibrationError): w.accept(bad)
        with self.assertRaises(wc.CalibrationError): w.accept({'/dev/cam-a': sample()['/dev/cam-a']})

    def test_only_success_is_persisted_and_reordered_config_keeps_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); w = self.wizard(); proof = self.finish(w)
            wc.save(base, proof)
            with patch.object(wc, 'boot_id', return_value='boot-a'), patch.object(wc, 'current_stamp', return_value=proof['stamp']):
                result = wc.apply(config(), base)
            self.assertEqual(result['wrist_cameras']['cam_left_wrist']['device'], '/dev/cam-b')
            self.assertEqual(result['wrist_cameras']['cam_right_wrist']['label'], '右腕相机')
            self.assertTrue(result['wrist_calibration_status']['valid'])

    def test_prior_manual_binding_is_not_proof_for_new_boot(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(wc, 'boot_id', return_value='boot-new'), patch.object(wc, 'current_stamp', return_value=[]):
            result = wc.apply(config(), Path(tmp))
            self.assertFalse(result['wrist_calibration_status']['valid'])
            self.assertFalse(result['wrist_cameras']['cam_left_wrist']['discovery']['role_confirmed'])

    def test_corrupt_file_requires_reconfirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(wc, 'boot_id', return_value='boot-a'), patch.object(wc, 'current_stamp', return_value=[]):
            p = Path(tmp)/'runtime'; p.mkdir(); (p/'wrist_calibration.json').write_text('{broken')
            self.assertFalse(wc.apply(config(), Path(tmp))['wrist_calibration_status']['valid'])

    def test_explicit_retry_invalidates_saved_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            proof=self.finish(self.wizard());wc.save(Path(tmp),proof);wc.invalidate(Path(tmp))
            self.assertFalse(wc.valid(wc.load(Path(tmp)), 'boot-a', proof['stamp']))

    def test_duplicate_role_paths_and_corrupt_roles_are_rejected(self):
        for roles in [{'cam_left_wrist':'/dev/cam-a','cam_right_wrist':'/dev/cam-a'}, [], {'cam_left_wrist':[]}]:
            proof=self.finish(self.wizard());proof['roles']=roles
            self.assertFalse(wc.valid(proof,'boot-a',proof['stamp']))

    def test_manual_preview_selection_resolves_both_orders_and_reuses_same_proof_rules(self):
        stamp=[['/dev/cam-a',1],['/dev/cam-b',2]]
        sources=dict(zip(wc.SLOTS,('/dev/cam-b','/dev/cam-a')))
        for slot in wc.SLOTS:
            proof=wc.manual_proof('boot-a',stamp,sources,slot)
            self.assertEqual(proof['roles']['cam_left_wrist'],sources[slot])
            self.assertEqual(proof['method'],'manual_preview')
            self.assertTrue(wc.valid(proof,'boot-a',stamp))
            self.assertFalse(wc.valid(proof,'boot-b',stamp))
            self.assertFalse(wc.valid(proof,'boot-a',[['/dev/cam-a',3],['/dev/cam-b',2]]))

    def test_manual_duplicate_missing_unknown_sources_and_unselected_side_are_rejected(self):
        stamp=[['/dev/cam-a',1],['/dev/cam-b',2]]
        for sources,slot in [
                ({wc.SLOTS[0]:'/dev/cam-a',wc.SLOTS[1]:'/dev/cam-a'},wc.SLOTS[0]),
                ({wc.SLOTS[0]:'/dev/cam-a'},wc.SLOTS[0]),
                ({wc.SLOTS[0]:'/dev/cam-a',wc.SLOTS[1]:'/dev/unexpected'},wc.SLOTS[0]),
                (dict(zip(wc.SLOTS,('/dev/cam-a','/dev/cam-b'))),None)]:
            with self.assertRaises(wc.CalibrationError):wc.manual_proof('boot-a',stamp,sources,slot)

    def test_manual_method_and_role_order_are_applied_to_recording_config(self):
        stamp=[['/dev/cam-a',1],['/dev/cam-b',2]]
        proof=wc.manual_proof('boot-a',stamp,dict(zip(wc.SLOTS,('/dev/cam-a','/dev/cam-b'))),wc.SLOTS[1])
        with tempfile.TemporaryDirectory() as tmp:
            wc.save(tmp,proof)
            with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=stamp):
                result=wc.apply(config(),tmp)
            self.assertEqual(result['wrist_calibration_status']['method'],'manual_preview')
            self.assertEqual(result['wrist_cameras'][wc.SLOTS[0]]['device'],'/dev/cam-b')

    def test_legacy_proof_without_method_keeps_occlusion_method(self):
        proof=self.finish(self.wizard());proof.pop('method')
        with tempfile.TemporaryDirectory() as tmp:
            wc.save(tmp,proof)
            with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=proof['stamp']):
                result=wc.apply(config(),tmp)
            self.assertEqual(result['wrist_calibration_status']['method'],'alternating_occlusion')


class ControllerCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Controller imports require the Pi ROS runtime, unlike the pure tests.
        if not Path('/opt/ros/noetic').exists():
            raise unittest.SkipTest('Controller integration runs in Pi ROS runtime')
        import project_paths
        import launcher
        cls.launcher=launcher

    def controller(self):
        c=self.launcher.Controller.__new__(self.launcher.Controller)
        c.state='device_confirm';c.worker=None;c.keyboard=None;c.quit=False;c.voice=Mock()
        c.state_write=Mock();c.poll=Mock();c.scan_ok=True;c.scan_available=list(wc.SLOTS)
        c.selected_devices=None;c.wrist_wizard=None;c.start_wrist_calibration=Mock()
        return c

    def test_device_confirm_enters_wizard_on_first_boot(self):
        c=self.controller();cfg=config();cfg['wrist_calibration_status']={'valid':False}
        with patch.object(self.launcher,'devices',return_value=cfg):c.key('a')
        c.start_wrist_calibration.assert_called_once()

    def test_next_session_same_boot_does_not_enter_wizard(self):
        c=self.controller();cfg=config();cfg['wrist_calibration_status']={'valid':True}
        with patch.object(self.launcher,'devices',return_value=cfg):c.key('a')
        c.start_wrist_calibration.assert_not_called();self.assertEqual(c.state,'idle')

    def test_prepare_cannot_start_worker_without_proof(self):
        c=self.controller();c.selected_devices=wc.SLOTS
        cfg=config();cfg['wrist_calibration_status']={'valid':False}
        with patch.object(self.launcher,'devices',return_value=cfg), patch.object(self.launcher.subprocess,'Popen',side_effect=AssertionError('must not start capture')):
            c.prepare()
        c.start_wrist_calibration.assert_called_once()

    def test_c_during_recording_never_reopens_cameras(self):
        c=self.controller();c.state='recording';c.worker=Mock()
        c.key('c');c.start_wrist_calibration.assert_not_called()

    def test_connection_change_cancels_in_progress_confirmation(self):
        c=self.controller();c.state='wrist_left';c.wrist_wizard=Mock();c.wrist_wizard.unchanged.return_value=False
        c.calibration_connection_changed=Mock();c.last_usb_poll=0
        with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=[]):c.poll_hotplug()
        c.calibration_connection_changed.assert_called_once()

    def test_probe_only_runs_after_user_a_and_not_with_worker(self):
        c=self.controller();c.state='wrist_baseline';c.wrist_wizard=Mock();c.advance_wrist_calibration=Mock()
        c.key('b');c.start_wrist_calibration.assert_called_once();c.advance_wrist_calibration.assert_not_called()
        c.key('a');c.advance_wrist_calibration.assert_called_once()

    def test_full_controller_flow_saves_swapped_roles_without_starting_recording(self):
        c=self.controller();c.selected_devices=wc.SLOTS
        w=wc.Wizard(config(),'boot-a',[['/dev/cam-a',1],['/dev/cam-b',2]])
        c.wrist_wizard=w;c.state='wrist_baseline';c.scan_devices=Mock();c.refresh_wrist_labels=Mock()
        cfg=config();cfg['wrist_calibration_status']={'valid':True}
        rows=[sample(),sample(100,1),sample(1,120),sample()]
        results=[Mock(returncode=0,stdout=json.dumps({'ok':True,'statistics':row,'boot_id':'boot-a','stamp':w.stamp})) for row in rows]
        with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=w.stamp), \
             patch.object(wc,'save') as save,patch.object(self.launcher,'devices',return_value=cfg), \
             patch.object(self.launcher.subprocess,'run',side_effect=results):
            for _ in range(4):c.advance_wrist_calibration()
        self.assertEqual(c.state,'idle');self.assertIsNone(c.worker)
        self.assertEqual(save.call_args.args[1]['roles']['cam_left_wrist'],'/dev/cam-b')

    def manual_controller(self,directory):
        c=self.controller();c.wrist_wizard=wc.Wizard(config(),'boot-a',[['/dev/cam-a',1],['/dev/cam-b',2]])
        c.state='wrist_manual';c.preview_dir=Path(directory);c.preview_worker=Mock();c.preview_worker.poll.return_value=None
        c.preview_snapshot_id='current-preview';c.preview_sources=dict(zip(wc.SLOTS,('/dev/cam-b','/dev/cam-a')))
        c.stop_preview=Mock();c.finish_wrist_calibration=Mock();c.calibration_connection_changed=Mock()
        for slot in wc.SLOTS:(c.preview_dir/(slot+'.jpg')).write_bytes(b'\xff\xd8\xff\xd9')
        return c

    def assignment(self,c):
        return dict(session_id=c.wrist_wizard.session_id,preview_id=c.preview_snapshot_id,left_slot=wc.SLOTS[0])

    def test_manual_confirmation_uses_displayed_sources_not_wizard_slot_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.manual_controller(tmp)
            with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=c.wrist_wizard.stamp):
                c.assign_wrist_roles(self.assignment(c))
            proof=c.finish_wrist_calibration.call_args.args[0]
            self.assertEqual(proof['roles'][wc.SLOTS[0]],'/dev/cam-b')
            self.assertEqual(proof['method'],'manual_preview');c.stop_preview.assert_called_once()

    def test_manual_stale_draft_missing_frame_or_stale_frame_never_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.manual_controller(tmp)
            with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=c.wrist_wizard.stamp):
                for key in ('session_id','preview_id'):
                    value=self.assignment(c);value[key]='old'
                    with self.assertRaises(ValueError):c.assign_wrist_roles(value)
                path=c.preview_dir/(wc.SLOTS[1]+'.jpg');path.unlink()
                with self.assertRaises(ValueError):c.assign_wrist_roles(self.assignment(c))
                path.write_bytes(b'jpeg');os.utime(str(path),(time.time()-10,time.time()-10))
                with self.assertRaises(ValueError):c.assign_wrist_roles(self.assignment(c))
            c.finish_wrist_calibration.assert_not_called();c.stop_preview.assert_not_called()

    def test_replug_during_confirmation_does_not_persist_old_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.manual_controller(tmp);stamp=c.wrist_wizard.stamp
            with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',side_effect=[stamp,[['/dev/cam-a',3],['/dev/cam-b',2]]]):
                with self.assertRaises(ValueError):c.assign_wrist_roles(self.assignment(c))
            c.finish_wrist_calibration.assert_not_called();c.calibration_connection_changed.assert_called_once()

    def test_manual_keyboard_a_never_measures_or_confirms_roles(self):
        c=self.controller();c.state='wrist_manual';c.wrist_wizard=Mock()
        c.advance_wrist_calibration=Mock();c.assign_wrist_roles=Mock();c.key('a')
        c.advance_wrist_calibration.assert_not_called();c.assign_wrist_roles.assert_not_called()

    def test_chooser_keyboard_a_enters_original_occlusion_flow(self):
        c=self.controller();c.state='wrist_choose';c.wrist_wizard=Mock();c.set_wrist_method=Mock()
        c.key('a');c.set_wrist_method.assert_called_once_with('occlusion')

    def test_method_switch_resets_partial_calibration_without_reopening_preview(self):
        c=self.controller();c.state='wrist_left';c.wrist_wizard=wc.Wizard(config(),'boot-a',[['/dev/cam-a',1],['/dev/cam-b',2]])
        old_session=c.wrist_wizard.session_id;c.stop_preview=Mock()
        with patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=c.wrist_wizard.stamp):
            c.set_wrist_method('manual');self.assertEqual(c.state,'wrist_manual')
            self.assertNotEqual(c.wrist_wizard.session_id,old_session)
            c.set_wrist_method('occlusion');self.assertEqual(c.state,'wrist_baseline')
        self.assertEqual(c.wrist_wizard.phase,'baseline');self.assertEqual(c.wrist_wizard.roles,{})
        c.stop_preview.assert_not_called()

    def test_recalibration_discards_previous_role_order_before_showing_preview(self):
        c=self.controller();c.dashboard=Mock();c.stop_preview=Mock();c.refresh_wrist_labels=Mock()
        old=config();old['wrist_cameras'][wc.SLOTS[0]]['device']='/dev/cam-b'
        old['wrist_cameras'][wc.SLOTS[1]]['device']='/dev/cam-a'
        fresh=config();fresh['wrist_calibration_status']={'valid':False}
        stamp=[['/dev/cam-a',1],['/dev/cam-b',2]]
        with patch.object(self.launcher,'devices',side_effect=[old,fresh]),patch.object(wc,'invalidate'), \
                patch.object(wc,'boot_id',return_value='boot-a'),patch.object(wc,'current_stamp',return_value=stamp):
            self.launcher.Controller.start_wrist_calibration(c)
        self.assertEqual(c.state,'wrist_choose');c.refresh_wrist_labels.assert_called_once_with(fresh)
        self.assertEqual(c.wrist_wizard.config['wrist_cameras'][wc.SLOTS[0]]['device'],'/dev/cam-a')

    def test_unconfirmed_labels_match_temporary_preview_source_order(self):
        c=self.controller();fresh=config();fresh['wrist_calibration_status']={'valid':False}
        fresh['wrist_cameras'][wc.SLOTS[0]]['label']='腕部相机 A'
        fresh['wrist_cameras'][wc.SLOTS[1]]['label']='腕部相机 B'
        c.last_scan=[{'name':wc.SLOTS[0],'device':'/dev/cam-b'}, {'name':wc.SLOTS[1],'device':'/dev/cam-a'}]
        with patch.object(self.launcher,'atomic_json'):c.refresh_wrist_labels(fresh)
        self.assertEqual(c.last_scan[0]['device'],'/dev/cam-a')
        self.assertEqual(c.last_scan[0]['label'],'腕部相机 A')
        self.assertFalse(c.last_scan[0]['role_confirmed'])


if __name__ == '__main__': unittest.main()
