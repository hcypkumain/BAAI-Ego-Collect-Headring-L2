"""Manual assignments must refer to the two live sources the operator saw."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard
import emg_calibration as ec
from live_preview import Publisher

STAMP=[['/dev/a','1a86','55d3','serial-a',1],['/dev/b','1a86','55d3','serial-b',2]]
def config():
    return {'emg_calibration':{'enabled':True,'scope':'boot'},'emg':{
        slot:dict(port=path,label='肌电 '+letter,discovery=dict(present=True,serial=serial,role_confirmed=False))
        for slot,path,letter,serial in [('emg_left','/dev/a','A','serial-a'),('emg_right','/dev/b','B','serial-b')]}}

class ManualActionTests(unittest.TestCase):
    def ui(self,state='emg_manual'):
        ui=dashboard.Dashboard.__new__(dashboard.Dashboard)
        ui.lock=threading.Lock();ui.busy=False;ui.prompting=False;ui.pending=None;ui.result=None
        ui.snapshot={'state':state,'revision':4}
        return ui

    def test_explicit_selection_is_required_and_old_measure_swap_are_removed(self):
        self.assertEqual(dashboard.actions_for('emg_manual'),['assign_emg_roles','restart_calibration'])
        self.assertNotIn('swap_emg',dashboard.actions_for('idle'))
        for value in (None,{},dict(session_id='s',preview_id='p',left_slot='/dev/a'),
                      dict(session_id='s',preview_id='p',left_slot='emg_left',port='/dev/a')):
            with self.assertRaises(ValueError):self.ui().submit(dict(action='assign_emg_roles',revision=4,value=value))

    def test_both_assignments_are_queued_but_recording_states_reject(self):
        for slot in ec.SLOTS:
            value=dict(session_id='s',preview_id='p',left_slot=slot)
            ui=self.ui();ui.submit(dict(action='assign_emg_roles',revision=4,value=value))
            self.assertEqual(ui.take()['value'],value)
        for state in ('preparing','ready','countdown','recording','confirm_stop','finalizing'):
            with self.assertRaises(ValueError):self.ui(state).submit(dict(action='assign_emg_roles',revision=4,value=value))

    def test_preview_identity_does_not_change_adc_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            pub=Publisher(tmp)
            try:
                pub.bind_emg_sources('preview-current',dict(emg_left='/dev/b'))
                values=[-8388608,-1,0,1,8388607,200,-300,5]
                frame=b'\xd2\xd2\xd2\xaa\x00'+b''.join(x.to_bytes(3,'big',signed=True) for x in values)
                with patch('live_preview.time.time',side_effect=[100,100.03]):
                    pub.emg('emg_left',frame);pub.emg('emg_left',frame)
                _,signals=pub._snapshot();signal=signals['emg_left']
                self.assertEqual(signal['preview_id'],'preview-current');self.assertEqual(signal['source_path'],'/dev/b')
                self.assertEqual(signal['rows'][0][1],[[v,v] for v in values])
            finally:pub.close()

    def test_frozen_recording_keeps_manual_proof_and_paths(self):
        proof=ec.manual_proof('boot',STAMP,dict(zip(ec.SLOTS,('/dev/a','/dev/b'))),'emg_right')
        cfg=config();cfg['emg_calibration_status']={'valid':True,'proof':proof}
        cfg['emg']['emg_left']['port']='/dev/b';cfg['emg']['emg_right']['port']='/dev/a'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'selection.json';path.write_text(json.dumps({'device_config':cfg}))
            with patch.dict(os.environ,{'ORANGEPI_DEVICE_SELECTION':str(path)}),patch.object(ec,'load',side_effect=AssertionError('must use frozen proof')):
                self.assertEqual(project_paths.devices(),cfg)

class ManualControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path('/opt/ros/noetic').exists():raise unittest.SkipTest('Pi ROS integration')
        import launcher
        cls.l=launcher

    def controller(self,tmp):
        c=self.l.Controller.__new__(self.l.Controller)
        c.state='emg_manual';c.worker=None;c.keyboard=None;c.quit=False;c.voice=Mock();c.poll=Mock();c.state_write=Mock()
        c.selected_devices=ec.SLOTS;c.scan_available=list(ec.SLOTS);c.scan_ok=True
        c.emg_wizard=ec.Wizard(config(),'boot',STAMP)
        c.preview_worker=Mock();c.preview_worker.poll.return_value=None
        c.preview_snapshot_id='current-preview';c.preview_dir=Path(tmp)
        # Deliberately reverse discovery order: the preview, not wizard slots, is authoritative.
        c.emg_preview_sources=dict(zip(ec.SLOTS,('/dev/b','/dev/a')))
        c.stop_preview=Mock();c.finish_emg_calibration=Mock();c.emg_connection_changed=Mock()
        self.write_signals(c)
        return c

    def write_signals(self,c,**overrides):
        for slot,path in c.emg_preview_sources.items():
            signal=dict(time=time.time(),count=200,rows=[[time.time(),[[0,1]]*8]],
                        source_path=path,preview_id=c.preview_snapshot_id)
            signal.update(overrides);(c.preview_dir/(slot+'.json')).write_text(json.dumps(signal))

    def value(self,c):
        return dict(session_id=c.emg_wizard.session_id,preview_id=c.preview_snapshot_id,left_slot='emg_left')

    def stamps(self):
        return patch.object(ec,'boot_id',return_value='boot'),patch.object(ec,'current_stamp',return_value=STAMP)

    def test_manual_confirmation_maps_displayed_source_with_no_probe_or_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.controller(tmp);boot,stamp=self.stamps()
            with boot,stamp,patch.object(self.l.subprocess,'run',side_effect=AssertionError('no automatic probe')):
                c.assign_emg_roles(self.value(c))
            proof=c.finish_emg_calibration.call_args[0][0]
            self.assertEqual(proof['roles'],dict(emg_left='/dev/b',emg_right='/dev/a'))
            self.assertEqual(proof['preview']['preview_id'],'current-preview');c.stop_preview.assert_called_once()

    def test_old_session_preview_or_unselected_never_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.controller(tmp);boot,stamp=self.stamps()
            with boot,stamp:
                for key,value in [('session_id','old'),('preview_id','old'),('left_slot',None)]:
                    draft=self.value(c);draft[key]=value
                    with self.assertRaises(ValueError):c.assign_emg_roles(draft)
            c.finish_emg_calibration.assert_not_called();c.stop_preview.assert_not_called()

    def test_stale_wrong_source_or_previous_preview_signal_never_saves(self):
        cases=[dict(time=time.time()-10),dict(time=time.time()+10),dict(source_path='/dev/other'),
               dict(preview_id='old'),dict(count=0),dict(rows=[])]
        for changes in cases:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as tmp:
                c=self.controller(tmp);self.write_signals(c,**changes);boot,stamp=self.stamps()
                with boot,stamp,self.assertRaises(ValueError):c.assign_emg_roles(self.value(c))
                c.finish_emg_calibration.assert_not_called()

    def test_missing_or_corrupt_one_channel_rejects_pair(self):
        for content in (None,'{}','{bad'):
            with tempfile.TemporaryDirectory() as tmp:
                c=self.controller(tmp);path=c.preview_dir/'emg_right.json'
                if content is None:path.unlink()
                else:path.write_text(content)
                boot,stamp=self.stamps()
                with boot,stamp,self.assertRaises(ValueError):c.assign_emg_roles(self.value(c))
                c.finish_emg_calibration.assert_not_called()

    def test_replug_during_confirm_prevents_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.controller(tmp);changed=copy.deepcopy(STAMP);changed[0][-1]=99
            with patch.object(ec,'boot_id',return_value='boot'),patch.object(ec,'current_stamp',side_effect=[STAMP,changed]):
                with self.assertRaises(ValueError):c.assign_emg_roles(self.value(c))
            c.finish_emg_calibration.assert_not_called();c.emg_connection_changed.assert_called_once()

    def test_dead_preview_or_recording_rejects_confirmation(self):
        for kind in ('dead','recording'):
            with tempfile.TemporaryDirectory() as tmp:
                c=self.controller(tmp)
                if kind=='dead':c.preview_worker.poll.return_value=1
                else:c.worker=Mock();c.state='recording'
                with self.assertRaises(ValueError):c.assign_emg_roles(self.value(c))
                c.finish_emg_calibration.assert_not_called()

    def test_keyboard_a_does_not_confirm_and_prepare_requires_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.controller(tmp);c.assign_emg_roles=Mock();c.key('a');c.assign_emg_roles.assert_not_called()
            c.state='idle';c.start_emg_calibration=Mock();c.needs_wrist_calibration=Mock(return_value=False)
            with patch.object(self.l,'devices',return_value=config()):c.prepare()
            c.start_emg_calibration.assert_called_once()

    def test_prepare_rechecks_exact_snapshot_if_proof_expires_after_first_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.controller(tmp);c.state='idle';c.smoke=False;c.session_dir=Path(tmp)
            c.images=[Path(tmp)/'unused-image.png'];c.start_emg_calibration=Mock()
            c.needs_emg_calibration=Mock(return_value=False);c.needs_wrist_calibration=Mock(return_value=False)
            before=set(Path(tmp).iterdir())
            with patch.object(self.l,'devices',return_value=config()),patch.object(self.l.storage_paths,'validate_output'),patch.object(self.l.subprocess,'Popen',side_effect=AssertionError('no capture')):
                c.prepare()
            c.start_emg_calibration.assert_called_once();self.assertEqual(set(Path(tmp).iterdir()),before)

if __name__=='__main__':unittest.main()
