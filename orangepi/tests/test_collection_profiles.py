"""Profile round-trips preserve original data and reject stale controls."""
import hashlib,json,pathlib,sys,tempfile,threading,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths,collection_profiles as profiles,device_discovery,dashboard,launcher


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.project=pathlib.Path(self.tmp.name);self.base=self.project/'orangepi'
        for path in ['configs','runtime','src','scripts','assets','.venvs']:(self.base/path).mkdir(parents=True,exist_ok=True)
        (self.project/'src').mkdir();(self.project/'configs/environments').mkdir(parents=True)
        (self.base/'configs/devices.yaml').write_text('eeg:\n  serial: CAP-13\nfirst_image: old.png\n')
        (self.base/'configs/storage.yaml').write_text('mode: sd\ndevice_id: test\n')
        (self.base/'configs/reference_control.json').write_text('{"version":1,"collector_id":"L2-013","selected_list_id":"pool"}')
        (self.base/'configs/task_block_policy.json').write_text('{"version":1,"list_ids":["legacy-task"],"block_size":"all"}')
        (self.base/'runtime/dashboard_token').write_text('a'*32)
        self.old_image=self.project/'configs/environments/scene_1.png';self.old_image.write_bytes(b'original-reference')
        (self.old_image.parent/'used.yaml').write_text('used: {scene_1.png: old-take}')
        (self.base/'runtime/reference_task_blocks.json').write_text('{"pending":"old-image"}')

    def test_round_trip_has_separate_libraries_progress_storage_and_collector(self):
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.project.rglob('*') if p.is_file()}
        value=profiles.initialize(self.base,'eeg');alt=profiles.profile_base(self.base,value,'emg')
        self.assertEqual(profiles.profile_base(self.base,value),self.base)
        self.assertEqual(list((alt.parent/'configs/environments').iterdir()),[])
        self.assertFalse((alt/'runtime/reference_task_blocks.json').exists())
        self.assertFalse((alt/'configs/task_block_policy.json').exists())
        self.assertEqual(json.loads((alt/'configs/reference_control.json').read_text())['collector_id'],'L2-013-EMG')
        (alt.parent/'configs/environments/scene_1.png').write_bytes(b'separate-reference')
        switched=profiles.activate(self.base,'emg',value)
        self.assertNotEqual(profiles.context(value),profiles.context(switched))
        back=profiles.activate(self.base,'eeg',switched)
        self.assertEqual(profiles.profile_base(self.base,back),self.base)
        self.assertTrue(all(hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest()==v for k,v in before.items()))
        self.assertEqual(profiles.storage_root('/sd/device',{}),pathlib.Path('/sd/device'))
        self.assertEqual(profiles.storage_root('/sd/device',{'capture_profile':'emg'}),pathlib.Path('/sd/device/profiles/emg'))
        with self.assertRaises(ValueError):profiles.storage_root('/sd/device',{'capture_profile':'../eeg'})
        with self.assertRaises(ValueError):profiles.activate(self.base,'emg',value)

    def test_emg_excludes_eeg_and_eeg_preserves_explicit_local_binding(self):
        cfg={'eeg':{'serial':'CAP-13','enabled':False},'emg':{'left':{}}}
        self.assertNotIn('eeg',profiles.device_config(cfg,'emg'))
        eeg=profiles.device_config(cfg,'eeg')['eeg'];self.assertTrue(eeg['enabled']);self.assertEqual(eeg['serial'],'CAP-13')
        self.assertFalse(cfg['eeg']['enabled'])

    def test_unbound_eeg_uses_only_a_single_local_cap(self):
        def cap(n):return dict(kind='eeg',serial='CAP-'+str(n),path='/dev/cap'+str(n),vendor='303a',product='1001')
        cfg={'eeg':{'enabled':True,'serial':''}}
        resolved,_=device_discovery.resolve(cfg,[cap(1)],{})
        self.assertEqual(resolved['eeg']['serial'],'CAP-1');self.assertEqual(resolved['eeg']['port'],'/dev/cap1')
        resolved,_=device_discovery.resolve(cfg,[cap(1),cap(2)],{})
        self.assertFalse(resolved['eeg']['discovery']['present']);self.assertEqual(resolved['eeg']['port'],'/dev/omnibci-unconnected')
        bound,_=device_discovery.resolve({'eeg':{'serial':'CAP-13','enabled':True}},[cap(1)],{})
        self.assertFalse(bound['eeg']['discovery']['present'])

    def test_old_mode_controls_cannot_start_cancel_or_discard_new_mode(self):
        d=dashboard.Dashboard.__new__(dashboard.Dashboard);d.lock=threading.Lock()
        d.snapshot={'collection_context':'emg:3','state':'recording','revision':1}
        d.privacy=Mock();d.camera_checks=Mock()
        for action in ['start','stop','discard_recording','prepare','collection_mode']:
            with self.subTest(action=action),self.assertRaisesRegex(ValueError,'模式已变化'):
                d.submit({'action':action,'revision':1,'collection_context':'eeg:2'})
        d.privacy.cancel_request.assert_not_called();d.camera_checks.approved.assert_not_called()

    def test_switch_rejected_with_live_owner_or_unconfirmed_stop(self):
        c=launcher.Controller.__new__(launcher.Controller);c.state='recording';c.worker=Mock()
        target='emg' if launcher.COLLECTION_MODE=='eeg' else 'eeg'
        with patch.object(profiles,'provision') as provision:
            with self.assertRaises(ValueError):c.switch_collection_mode(target)
            provision.assert_not_called()
            c.state='idle';c.worker=None;c.dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':True}))
            with self.assertRaises(ValueError):c.switch_collection_mode(target)
            provision.assert_not_called()

    def test_persistent_media_catalog_rebinds_before_reading_other_mode(self):
        import importlib.util
        path=pathlib.Path(__file__).resolve().parents[2]/'services/phone-media/replay_runtime.py'
        spec=importlib.util.spec_from_file_location('profile_replay_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        mount=self.project/'sd';mount.mkdir()
        import yaml
        (self.base/'configs/storage.yaml').write_text(yaml.safe_dump(dict(mode='sd',mount_point=str(mount),expected_uuid='TEST',directory='capture',device_id='pi')))
        value=profiles.initialize(self.base,'eeg');runtime=module.ReplayRuntime(self.base)
        found=SimpleNamespace(returncode=0,stdout=json.dumps({'filesystems':[{'target':str(mount),'uuid':'TEST'}]}))
        with patch.object(module.subprocess,'run',return_value=found):
            old=runtime._storage();old_catalog=Mock();runtime.instance=old_catalog
            profiles.activate(self.base,'emg',value)
            new=runtime._storage()
        self.assertEqual(old,mount/'capture/pi/data');self.assertEqual(new,mount/'capture/pi/profiles/emg/data')
        old_catalog.release.assert_called_once();self.assertIsNone(runtime.instance)


class ManualCompletionTests(unittest.TestCase):
    def test_failed_stop_is_returned_to_phone_as_failed_discard(self):
        c=launcher.Controller.__new__(launcher.Controller);c.state='recording';c.ui_revision=8
        c.selected_devices=[];c.keyboard=None;c.voice=Mock();c.ui_publish=Mock();c.ensure_preview=Mock();c.worker_status=lambda:{}
        c.dashboard=Mock();c.dashboard.take.return_value={'action':'discard_recording','revision':8}
        c.discard_recording=Mock(return_value=False);c.owner_command_error='停止回执未确认'
        c.dashboard_tick();c.dashboard.complete.assert_called_once_with('停止回执未确认')

    def test_pending_qc_completion_waits_for_manual_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            path=pathlib.Path(temp);(path/'capture_summary.json').write_text(json.dumps({'status':'pending_qc','storage_durability':{'completed':True}}))
            (path/'meta.yaml').write_text('{}')
            c=launcher.Controller.__new__(launcher.Controller)
            c.path=path;c.state='finalizing';c.worker=Mock();c.worker.poll.return_value=0;c.worker.returncode=0
            c.worker_status=lambda:{};c.voice=Mock();c.notified_error=False;c.warned_data_gap=False
            c.quit=False;c.smoke=False;c.image=path/'scene_1.png';c.retry_requested=False
            c.reference_session_metadata=lambda:{};c.state_write=Mock();c.prepare=Mock();c.error_voice=Mock()
            c.reap=lambda:setattr(c,'worker',None)
            with patch.object(launcher.ledger,'mark'):c.poll()
            self.assertEqual(c.state,'idle');self.assertEqual(c.last_completed['outcome'],'pending_qc')
            from capture_session_state import active_session
            self.assertIsNone(active_session(c));self.assertEqual(c.last_review_path,path);self.assertEqual(c.path,path)
            c.prepare.assert_not_called();c.error_voice.assert_not_called()

    def test_live_discard_never_deletes_before_stop_is_confirmed(self):
        c=launcher.Controller.__new__(launcher.Controller);c.state='recording'
        c.stop_before_prompt=Mock(return_value=False);c.discard_and_retry=Mock()
        self.assertFalse(c.discard_recording());c.discard_and_retry.assert_not_called()
        c.stop_before_prompt.side_effect=lambda:(setattr(c,'state','confirm_stop') or True)
        c.discard_recording();c.discard_and_retry.assert_called_once();self.assertTrue(c.discard_return_overview)


if __name__=='__main__':unittest.main()
