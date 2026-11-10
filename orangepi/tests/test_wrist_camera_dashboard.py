"""Software-only policy, controller ownership and frozen configuration checks."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import dashboard
import web_controls
import wrist_camera_settings as settings


class ActionTests(unittest.TestCase):
    def ui(self, state='idle', can_change=True):
        ui=dashboard.Dashboard.__new__(dashboard.Dashboard)
        ui.lock=threading.Lock();ui.busy=False;ui.prompting=False
        ui.pending=None;ui.result=None
        ui.privacy=Mock();ui.privacy.snapshot.return_value={'required':False,'stop_confirmed':True,'phase':'idle'}
        ui.snapshot={'state':state,'revision':4,
                     'wrist_camera_settings':{'can_change':can_change}}
        return ui

    def test_all_three_explicit_choices_are_accepted(self):
        for value in (None,False,True):
            with self.subTest(value=value):
                ui=self.ui();ui.submit({'action':'wrist_camera_settings','revision':4,'value':value})
                self.assertIs(ui.take()['value'],value)

    def test_missing_and_non_boolean_choices_are_rejected(self):
        for value in (0,1,'false','null',[],{}):
            ui=self.ui()
            with self.assertRaises(ValueError):ui.submit({'action':'wrist_camera_settings','revision':4,'value':value})
            self.assertIsNone(ui.take())
        with self.assertRaises(ValueError):self.ui().submit({'action':'wrist_camera_settings','revision':4})

    def test_only_safe_states_allow_configuration(self):
        for state in dashboard.SAFE:
            self.assertIn('wrist_camera_settings',dashboard.actions_for(state))
        for state in ('preparing','countdown','recording','confirm_stop','finalizing','scanning','wrist_manual'):
            with self.subTest(state=state):
                ui=self.ui(state)
                with self.assertRaises(ValueError):ui.submit({'action':'wrist_camera_settings','revision':4,'value':False})

    def test_external_capture_and_stale_revisions_cannot_queue(self):
        for ui,revision in ((self.ui(can_change=False),4),(self.ui(),3)):
            with self.assertRaises(ValueError):ui.submit({'action':'wrist_camera_settings','revision':revision,'value':False})
            self.assertIsNone(ui.take())


class ControllerTests(unittest.TestCase):
    def controller(self, state='idle'):
        c=web_controls.WebControls()
        c.state=state;c.ui_revision=4;c.worker=None;c.keyboard=None;c.camera_registry=object()
        c.voice=Mock();c.OwnerExited=type('OwnerExited',(RuntimeError,),{})
        c.worker_status=lambda:{}
        c.dashboard=Mock();c.dashboard.take.return_value={'action':'wrist_camera_settings','revision':4,'value':False}
        c.ui_publish=Mock();c.ensure_preview=Mock();c.state_write=Mock()
        c.release_prepared=Mock();c.stop_preview=Mock()
        c.wrist_settings_available=Mock(return_value=True)
        def run_settings(command,registry,*,kind,input_text,timeout):
            self.assertEqual(kind,'settings')
            try:
                with settings.preference_lock(web_controls.BASE),settings.capture_guard(web_controls.BASE):
                    reports=settings.configure_devices(web_controls.devices(),json.loads(input_text))
                    web_controls.WebControls().save_wrist_settings(json.loads(input_text),reports)
                return json.dumps({'ok':True,'reports':reports})
            except Exception as error:return json.dumps({'ok':False,'error':str(error),'reports':getattr(error,'reports',{})})
        patcher=patch.object(web_controls,'run_scan',side_effect=run_settings);patcher.start();self.addCleanup(patcher.stop)
        return c

    @contextmanager
    def gate(self, events, name):
        events.append(name+' enter')
        try:yield
        finally:events.append(name+' exit')

    def test_ready_releases_before_hardware_changes_and_saves_after_readback(self):
        c=self.controller('ready');events=[];cfg={'wrist_cameras':{}}
        c.release_prepared.side_effect=lambda:events.append('release')
        c.stop_preview.side_effect=lambda:events.append('stop preview')
        c.state_write.side_effect=lambda:events.append('state')
        def configure(config,value):
            self.assertEqual(config,cfg);self.assertIs(value,False)
            events.append('configure');return {'cam_left_wrist':{'actual_allow_auto_exposure_fps_change':False}}
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate(events,'preference')), \
             patch.object(settings,'capture_guard',side_effect=lambda base:self.gate(events,'capture')), \
             patch.object(settings,'configure_devices',side_effect=configure), \
             patch.object(settings,'save',side_effect=lambda base,value:events.append('save')), \
             patch.object(web_controls,'devices',return_value=cfg):
            c.dashboard_tick()
        self.assertEqual(events,['release','stop preview','preference enter','capture enter',
            'configure','save','capture exit','preference exit','state'])
        c.dashboard.complete.assert_called_once_with(None)
        c.ensure_preview.assert_called_once()

    def test_failed_readback_keeps_preferences_and_exposes_failure_report(self):
        c=self.controller();error=ValueError('right wrist refused setting')
        error.reports={'cam_right_wrist':{'error':str(error)}}
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_guard',side_effect=lambda base:self.gate([],'capture')), \
             patch.object(settings,'configure_devices',side_effect=error), \
             patch.object(settings,'save') as save,patch.object(web_controls,'devices',return_value={}):
            c.dashboard_tick()
        save.assert_not_called();c.state_write.assert_not_called()
        self.assertEqual(c.wrist_camera_last_reports,error.reports)
        c.dashboard.complete.assert_called_once_with(str(error));c.ensure_preview.assert_called_once()

    def test_save_failure_restores_measured_value_when_old_preference_was_preserve(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);settings.save(root,None);c=self.controller();events=[]
            slot='cam_left_wrist'
            reports={slot:{'device':'/dev/a','requested':False,'applied':True,
                'actual_allow_auto_exposure_fps_change':False,
                'previous_actual_allow_auto_exposure_fps_change':True}}
            def restore(original):
                events.append('restore')
                self.assertIs(original[slot]['previous_actual_allow_auto_exposure_fps_change'],True)
                return {slot:{'requested':True,'applied':True,'error':None,
                              'actual_allow_auto_exposure_fps_change':True}}
            with patch.object(web_controls,'BASE',root), \
                 patch.object(settings,'preference_lock',side_effect=lambda base:self.gate(events,'preference')), \
                 patch.object(settings,'capture_guard',side_effect=lambda base:self.gate(events,'capture')), \
                 patch.object(settings,'configure_devices',return_value=reports), \
                 patch.object(settings,'save',side_effect=OSError('disk is full')), \
                 patch.object(settings,'restore_devices',side_effect=restore,create=True), \
                 patch.object(web_controls,'devices',return_value={}):
                c.dashboard_tick()
            self.assertEqual(events,['preference enter','capture enter','restore','capture exit','preference exit'])
            self.assertIs(settings.load(root)[settings.KEY],None)
            self.assertIs(c.wrist_camera_last_reports[slot]['actual_allow_auto_exposure_fps_change'],True)
            self.assertTrue(c.wrist_camera_last_reports[slot]['rolled_back'])
            self.assertFalse(c.wrist_camera_last_change['ok'])
            self.assertIn('未保存',c.wrist_camera_last_change['error'])
            c.state_write.assert_not_called();c.ensure_preview.assert_called_once()

    def test_save_and_rollback_failure_are_both_reported_without_success(self):
        c=self.controller();slot='cam_right_wrist'
        reports={slot:{'device':'/dev/b','requested':False,'applied':True,
            'actual_allow_auto_exposure_fps_change':False,
            'previous_actual_allow_auto_exposure_fps_change':True}}
        failure=ValueError('device refused rollback')
        failure.reports={slot:{'requested':True,'applied':False,'error':str(failure),
                              'actual_allow_auto_exposure_fps_change':False}}
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_guard',side_effect=lambda base:self.gate([],'capture')), \
             patch.object(settings,'configure_devices',return_value=reports), \
             patch.object(settings,'save',side_effect=OSError('read-only filesystem')), \
             patch.object(settings,'restore_devices',side_effect=failure,create=True), \
             patch.object(web_controls,'devices',return_value={}):
            c.dashboard_tick()
        message=c.dashboard.complete.call_args.args[0]
        self.assertIn('未保存',message);self.assertIn('恢复原设置失败',message)
        self.assertFalse(c.wrist_camera_last_reports[slot]['rolled_back'])
        self.assertEqual(c.wrist_camera_last_reports[slot]['rollback_error'],str(failure))
        self.assertFalse(c.wrist_camera_last_change['ok']);c.state_write.assert_not_called()

    def test_preserve_save_failure_performs_no_control_write_or_false_restore_claim(self):
        c=self.controller();c.dashboard.take.return_value['value']=None;slot='cam_left_wrist'
        reports={slot:{'device':'/dev/a','requested':None,'applied':False,
            'actual_allow_auto_exposure_fps_change':True,
            'previous_actual_allow_auto_exposure_fps_change':True}}
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_guard',side_effect=lambda base:self.gate([],'capture')), \
             patch.object(settings,'configure_devices',return_value=reports), \
             patch.object(settings,'save',side_effect=OSError('disk full')), \
             patch.object(settings,'_command',side_effect=AssertionError('preserve must not write')) as command, \
             patch.object(web_controls,'devices',return_value={}):
            c.dashboard_tick()
        command.assert_not_called()
        self.assertIs(c.wrist_camera_last_reports[slot]['actual_allow_auto_exposure_fps_change'],True)
        self.assertTrue(c.wrist_camera_last_reports[slot]['rollback_skipped'])
        self.assertFalse(c.wrist_camera_last_reports[slot]['rolled_back'])
        self.assertFalse(c.wrist_camera_last_change['ok'])

    def test_capture_started_since_click_rejects_without_releasing_or_writing(self):
        c=self.controller();c.wrist_settings_available.return_value=False
        with patch.object(settings,'configure_devices') as configure,patch.object(settings,'save') as save:
            c.dashboard_tick()
        c.release_prepared.assert_not_called();c.stop_preview.assert_not_called()
        configure.assert_not_called();save.assert_not_called()
        self.assertIn('暂不可更改',c.dashboard.complete.call_args.args[0])

    def test_race_at_capture_guard_does_not_apply_or_save(self):
        c=self.controller()
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_guard',side_effect=ValueError('Capture is busy')), \
             patch.object(settings,'configure_devices') as configure,patch.object(settings,'save') as save:
            c.dashboard_tick()
        configure.assert_not_called();save.assert_not_called()
        c.dashboard.complete.assert_called_once_with('Capture is busy')

    def test_ready_own_worker_is_allowed_but_external_capture_is_locked(self):
        c=web_controls.WebControls();c.state='ready';c.worker=Mock()
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_available',return_value=False) as available:
            self.assertTrue(c.wrist_settings_available());available.assert_not_called()
            c.state='idle';c.worker=None
            self.assertFalse(c.wrist_settings_available());available.assert_called_once()
        with patch.object(settings,'preference_lock',side_effect=ValueError('handoff')):
            self.assertFalse(c.wrist_settings_available())

    def test_external_capture_prevents_idle_preview_from_reopening_devices(self):
        c=web_controls.WebControls();c.dashboard=Mock();c.worker=None;c.state='idle';c.preview_worker=None
        with patch.object(settings,'preference_lock',side_effect=lambda base:self.gate([],'preference')), \
             patch.object(settings,'capture_available',return_value=False), \
             patch.object(web_controls.subprocess,'Popen') as spawn:
            c.ensure_preview()
        spawn.assert_not_called()

    def test_reports_follow_current_capture_or_preview_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);preview=root/'preview';preview.mkdir();capture=root/'capture'
            slot='cam_left_wrist';(capture/slot).mkdir(parents=True)
            preview_report={'requested':None,'actual_allow_auto_exposure_fps_change':True}
            capture_report={'requested':False,'actual_allow_auto_exposure_fps_change':False}
            (preview/('wrist-controls-'+slot+'.json')).write_text(json.dumps(preview_report))
            (capture/slot/'camera_controls.json').write_text(json.dumps(capture_report))
            c=web_controls.WebControls();c.state='idle';c.worker=None;c.preview_dir=preview;c.path=capture
            c.wrist_camera_last_change={'ok':False,'error':'disk full','devices':{'cam_left_wrist':{'rolled_back':True}}}
            with patch.object(c,'wrist_settings_available',return_value=True),patch.object(settings,'load',return_value={settings.KEY:None}):
                status=c.wrist_settings_status()
                self.assertEqual(status['devices'][slot],preview_report);self.assertEqual(status['report_source'],'preview')
                self.assertEqual(status['last_change'],c.wrist_camera_last_change)
                c.state='recording';c.worker=Mock()
                status=c.wrist_settings_status()
                self.assertEqual(status['devices'][slot],capture_report);self.assertEqual(status['report_source'],'capture')


class ConfigurationTests(unittest.TestCase):
    def test_capture_snapshot_does_not_load_new_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'selection.json'
            frozen={'wrist_camera_settings':{settings.KEY:True},'wrist_cameras':{'cam_left_wrist':{settings.KEY:True}}}
            path.write_text(json.dumps({'selected':['cam_left_wrist'],'device_config':frozen}))
            with patch.dict('os.environ',{'ORANGEPI_DEVICE_SELECTION':str(path)}), \
                 patch.object(settings,'load',side_effect=AssertionError('snapshot changed')):
                self.assertEqual(project_paths.devices(),frozen)

    def test_normal_configuration_adds_preference_to_both_wrists(self):
        import device_discovery
        import wrist_calibration
        import emg_calibration
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'configs').mkdir()
            cfg={'wrist_cameras':{'cam_left_wrist':{'device':'/dev/a'},'cam_right_wrist':{'device':'/dev/b'}}}
            (root/'configs/devices.yaml').write_text(json.dumps(cfg))
            settings.save(root,False)
            with patch.object(project_paths,'BASE',root),patch.dict('os.environ',{'ORANGEPI_DEVICE_SELECTION':''}), \
                 patch.object(device_discovery,'discover',side_effect=lambda config,base:config), \
                 patch.object(wrist_calibration,'apply',side_effect=lambda config,base:config), \
                 patch.object(emg_calibration,'apply',side_effect=lambda config,base:config):
                resolved=project_paths.devices()
            self.assertIs(resolved['wrist_camera_settings'][settings.KEY],False)
            self.assertTrue(all(item[settings.KEY] is False for item in resolved['wrist_cameras'].values()))
            self.assertNotIn(settings.KEY,cfg['wrist_cameras']['cam_left_wrist'])


if __name__=='__main__':unittest.main()
