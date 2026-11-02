"""Clicks during real prompt playback and the completed calibration transition."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import cloud_speaker
import dashboard
import launcher
import wrist_calibration as wc
from test_boot_calibration import config, sample


class PromptFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name); (self.base/'runtime').mkdir()
        self.ui = dashboard.Dashboard(self.base, self.base, {'host':'127.0.0.1', 'port':0})

    def tearDown(self):
        self.ui.close(); self.temp.cleanup()

    def speaker(self):
        voice = cloud_speaker.Speaker.__new__(cloud_speaker.Speaker)
        voice.silent=False; voice.cancelled=lambda:False; voice.env={}
        voice.cloud=Mock(); voice.cloud.config={'foreground_wait_seconds':8,'wait_prompt_after_seconds':3}
        voice.cloud.request.return_value=Path('cached-prompt.wav')
        voice.cloud.last_error.return_value=None
        voice.on_prompt=self.ui.set_prompting
        voice.skip_requested=self.ui.has_pending
        return voice

    def test_next_click_interrupts_real_playback_and_stays_queued_once(self):
        self.ui.publish({'state':'wrist_left','revision':8}); self.ui.busy=True
        voice=self.speaker(); player=Mock(); player.poll.side_effect=[None, None, 0]
        def click(_):
            self.assertIn('measure', self.ui.status()['actions'])
            self.ui.submit({'action':'measure','revision':8})
            with self.assertRaises(ValueError):self.ui.submit({'action':'measure','revision':8})
        with patch('cloud_speaker.subprocess.Popen',return_value=player), patch('cloud_speaker.time.sleep',side_effect=click):
            voice.say('遮住左腕后点击检测')
        player.terminate.assert_called_once()
        self.ui.complete()
        self.assertEqual(self.ui.take(),{'action':'measure','revision':8})
        self.assertIsNone(self.ui.take())

    def test_measurement_busy_cannot_be_skipped_as_if_it_were_speech(self):
        self.ui.publish({'state':'wrist_left','revision':8}); self.ui.busy=True
        self.assertEqual(self.ui.status()['actions'],[])
        with self.assertRaises(ValueError):self.ui.submit({'action':'measure','revision':8})

    def test_old_step_click_during_prompt_still_rejected(self):
        self.ui.publish({'state':'wrist_right','revision':9}); self.ui.busy=True
        self.ui.set_prompting(True)
        with self.assertRaises(ValueError):self.ui.submit({'action':'measure','revision':8})
        self.assertFalse(self.ui.has_pending())

    def test_pending_next_step_skips_remaining_old_prompt_batch(self):
        self.ui.publish({'state':'idle','revision':9})
        self.ui.submit({'action':'prepare','revision':9})
        voice=self.speaker(); voice.say('过时的第二条提示')
        voice.cloud.request.assert_not_called()

    def test_cloud_wait_can_be_skipped_without_becoming_audio_failure(self):
        self.ui.publish({'state':'idle','revision':9}); self.ui.busy=True
        voice=self.speaker(); voice.cloud.request.return_value=None
        def request(text,wait=0):
            if wait:self.ui.submit({'action':'prepare','revision':9})
        voice.cloud.request.side_effect=request
        voice.say('等待云端合成')
        self.assertTrue(self.ui.has_pending())

    def test_countdown_and_saving_do_not_gain_actions_from_speech(self):
        for state in ('countdown','finalizing','scanning'):
            self.ui.publish({'state':state,'revision':1}); self.ui.set_prompting(True)
            self.assertEqual(self.ui.status()['actions'],[])

    def test_preparing_becomes_ready_while_reference_prompt_is_playing(self):
        c=launcher.Controller.__new__(launcher.Controller)
        c.state='preparing';c.worker=Mock();c.worker.poll.return_value=None
        c.worker_status=Mock(return_value={'state':'ready'});c.dashboard=self.ui
        c.state_write=Mock();c.ui_publish=Mock()
        self.assertFalse(c.voice_skip_requested())
        self.assertEqual(c.state,'ready');c.state_write.assert_called_once()


class CalibrationCompletionTests(unittest.TestCase):
    def test_label_or_order_change_preserves_selection_but_device_loss_does_not(self):
        rows=[{'name':slot,'label':'临时相机','device':'/dev/'+slot,
               'connected':True,'present':True} for slot in wc.SLOTS]
        for device_lost in (False,True):
            with self.subTest(device_lost=device_lost):
                c=launcher.Controller.__new__(launcher.Controller)
                c.state='idle';c.last_scan=copy.deepcopy(rows);c.selected_devices=wc.SLOTS
                c.stop_preview=Mock();c.state_write=Mock();c.voice=Mock()
                updated=list(reversed(copy.deepcopy(rows)))
                for row in updated:row['label']='已确认'+row['name']
                if device_lost:updated[0]['connected']=False
                result=Mock(returncode=0,stdout=json.dumps(updated))
                with patch.object(launcher,'devices',return_value={}), \
                     patch.object(launcher,'fingerprint',return_value=()), \
                     patch.object(launcher,'atomic_json'), \
                     patch.object(launcher.subprocess,'run',return_value=result):
                    c.scan_devices(announce=False)
                self.assertEqual(c.state,'device_confirm' if device_lost else 'idle')
                self.assertEqual(c.selected_devices,None if device_lost else wc.SLOTS)
                if not device_lost:c.voice.say.assert_not_called()

    def test_completion_does_not_return_to_scan_or_clear_confirmed_selection(self):
        c=launcher.Controller.__new__(launcher.Controller)
        cfg=config(); cfg['wrist_calibration_status']={'valid':True}
        w=wc.Wizard(cfg,'boot-a',[['/dev/cam-a',1],['/dev/cam-b',2]])
        for row in (sample(),sample(100,1),sample(1,120)):w.accept(row)
        c.wrist_wizard=w;c.state='wrist_restore';c.worker=None;c.voice=Mock()
        c.stop_preview=Mock();c.selected_devices=wc.SLOTS;c.scan_available=list(wc.SLOTS)
        c.last_scan=[{'name':slot,'label':'临时相机','device':cfg['wrist_cameras'][slot]['device'],
                     'connected':True,'present':True,'config':cfg['wrist_cameras'][slot]} for slot in wc.SLOTS]
        states=[];c.state_write=lambda:states.append(c.state)
        resolved=copy.deepcopy(cfg)
        resolved['wrist_cameras']['cam_left_wrist']['device']='/dev/cam-b'
        resolved['wrist_cameras']['cam_right_wrist']['device']='/dev/cam-a'
        result=Mock(returncode=0,stdout=json.dumps({'ok':True,'statistics':sample(),'boot_id':'boot-a','stamp':w.stamp}))
        def run(*args,**kwargs):
            self.assertTrue(str(args[0][-1]).endswith('calibration_probe.py'),'completion must not reopen devices for another scan')
            return result
        with patch.object(wc,'boot_id',return_value='boot-a'), patch.object(wc,'current_stamp',return_value=w.stamp), \
             patch.object(wc,'save'), patch.object(launcher,'devices',return_value=resolved), \
             patch('web_controls.devices',return_value=resolved),patch.object(launcher,'atomic_json'), \
             patch.object(launcher.subprocess,'run',side_effect=run):
            c.advance_wrist_calibration()
        self.assertEqual(c.state,'idle');self.assertNotIn('scanning',states);self.assertNotIn('device_confirm',states)
        self.assertEqual(c.selected_devices,wc.SLOTS)
        self.assertTrue(c.web_config['wrist_calibration_status']['valid'])
        self.assertEqual(c.last_scan[0]['device'],'/dev/cam-b')


if __name__=='__main__':unittest.main()
