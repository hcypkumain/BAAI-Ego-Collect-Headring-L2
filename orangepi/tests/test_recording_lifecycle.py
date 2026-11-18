"""Retained takes, reversible deletion, reference ownership and capture exits."""
import copy,datetime,hashlib,importlib.util,json,os,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
COMMON=Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
sys.path.insert(0,str(COMMON));import project_paths
from recording_store import RecordingStore
import ledger,dashboard
from web_controls import WebControls
from reference_controls import ReferenceControlsMixin
from required_capture_fixture import write_session

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.project=self.root/'project';self.base=self.project/'orangepi';self.base.mkdir(parents=True)
        self.library=self.project/'configs/environments/scene';self.library.mkdir(parents=True);self.image=self.library/'task_1.png';self.image.write_bytes(b'reference')
        self.volume=self.root/'volume';self.sessions=self.volume/'data/session-night';self.sessions.mkdir(parents=True)
        self.now=1793318400.;self.store=RecordingStore(self.base,self.project,ledger,lambda:self.sessions,lambda:self.volume,lambda:self.now)
        self.patches=[patch.object(ledger,'BASE',self.base),patch.object(ledger,'PROJECT',self.project),patch.object(ledger,'LIBRARY',self.project/'configs/environments')]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def take(self,name='L2-001-2026-10-30-10-00-00',status='success',keep=True,start=None,duration=10):
        path=self.sessions/name
        start=self.now if start is None else start
        write_session(path,level='WARN' if status=='success' else 'ERROR',
            summary_status=status,start_unix_ns=int(start*1e9),duration_ns=int(duration*1e9))
        # Preserve the legacy top-level video artifact used by the deletion
        # custody assertion; qualification evidence lives in stream folders.
        (path/'frames.mp4').write_bytes(b'preserved-video')
        value=__import__('yaml').safe_load((path/'meta.yaml').read_text())
        value.update(status=status,retention_state='kept' if keep else 'unconfirmed',
            reference_image='configs/environments/scene/task_1.png',session_created_unix=start,duration=duration)
        (path/'meta.yaml').write_text(__import__('yaml').safe_dump(value))
        if keep and status=='success':ledger.mark(self.image,path,'success')
        return path
    def test_retained_take_has_collected_reference_and_upload_entry(self):
        path=self.take();value=self.store.page();self.assertEqual(value['retained_count'],1);self.assertEqual(value['items'][0]['reference_state'],'collected')
        upload=self.store.upload_manifest();self.assertEqual(len(upload['recordings']),1);self.assertIn(path.name,upload['recordings'][0]['relative_directory'])
    def test_unconfirmed_or_failed_takes_are_not_uploadable(self):
        self.take(status='cancelled',keep=False);self.assertEqual(self.store.page()['retained_count'],0);self.assertEqual(self.store.upload_manifest()['recordings'],[])
    def test_cross_midnight_take_belongs_to_confirmation_date(self):
        import yaml
        now=datetime.datetime(2026,10,30,0,1,tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp();self.now=now
        path=self.take(start=now-120,duration=60);meta=yaml.safe_load((path/'meta.yaml').read_text());meta['retained_at_unix']=now
        (path/'meta.yaml').write_text(yaml.safe_dump(meta))
        self.assertEqual(self.store.page('today')['retained_count'],1);self.assertEqual(self.store.page('today')['day'],'2026-10-30')
    def test_retaining_error_preserves_quality_and_leaves_reference_uncollected(self):
        path=self.take(status='error',keep=False);summary=json.loads((path/'capture_summary.json').read_text());summary['cam_ros_left']={'frames':1};summary['qc_level']='ERROR';(path/'capture_summary.json').write_text(json.dumps(summary));ledger.mark(self.image,path,'error')
        raw=(path/'capture_summary.json').read_bytes();row=self.store.page()['items'][0];self.store.retain_error(path.name,row['token'])
        row=self.store.page()['items'][0];self.assertTrue(row['retained']);self.assertEqual(row['retention_kind'],'unsuccessful');self.assertFalse(row['reference_attempt_succeeded']);self.assertEqual(row['reference_state'],'uncollected');self.assertEqual(row['qc_level'],'ERROR')
        self.assertEqual((path/'capture_summary.json').read_bytes(),raw);self.assertEqual(self.store.upload_manifest()['recordings'],[])
    def test_fresh_images_precede_error_images_and_warn_is_success(self):
        fresh=self.library/'task_2.png';fresh.write_bytes(b'new');warn=self.library/'task_3.png';warn.write_bytes(b'warn')
        error=self.take(status='error',keep=False);(error/'capture_summary.json').write_text(json.dumps({'status':'error','cam_ros_left':{'frames':1}}));ledger.mark(self.image,error,'error')
        good=self.sessions/'good';write_session(good)
        meta=__import__('yaml').safe_load((good/'meta.yaml').read_text());meta['reference_image']='configs/environments/scene/task_3.png'
        (good/'meta.yaml').write_text(__import__('yaml').safe_dump(meta));ledger.mark(warn,good,'success')
        control=ReferenceControlsMixin();control._reference_project=self.project;control._reference_ledger=ledger;control.refresh_reference_histories()
        self.assertTrue(control._reference_histories[warn.relative_to(self.project).as_posix()]['collected'])
        entries=[{'path':self.image},{'path':fresh}]
        with patch('reference_controls.secrets.choice',side_effect=lambda pool:pool[0]):self.assertEqual(control._reference_entry_choice(entries)['path'],fresh)
        ledger.release(warn,good);control.refresh_reference_histories()
        with patch('reference_controls.secrets.choice',side_effect=lambda pool:pool[0]):self.assertEqual(control._reference_entry_choice([{'path':warn},{'path':self.image}])['path'],self.image)
    def test_recorded_but_unconfirmed_current_take_is_not_labeled_never_recorded(self):
        path=self.take(status='error',keep=False);(path/'start_gate.json').write_text('{}')
        control=ReferenceControlsMixin();control.image=self.image;control.path=path;control.worker=Mock();control.state='confirm_stop';control._reference_project=self.project;control._reference_histories={};control._reference_snapshot={'items':[]};control.worker_status=Mock(return_value={'state':'error'})
        value=control.reference_image_state();self.assertTrue(value['pending_current_result']);self.assertTrue(value['ever_recorded']);self.assertEqual(value['current_qc'],'ERROR')
    def test_delete_moves_all_files_and_releases_reference(self):
        path=self.take();token=self.store.page()['items'][0]['token'];result=self.store.delete(path.name,token)
        self.assertTrue(result['reference_released']);self.assertFalse(path.exists());self.assertEqual(self.store.page()['total'],0)
        copies=list((self.volume/'deleted-recordings').glob('*/frames.mp4'));self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_bytes(),b'preserved-video')
        self.assertEqual(ledger.read(self.library/'used.yaml')['used'],{});self.assertEqual(self.store.upload_manifest()['recordings'],[])
    def test_stale_token_and_traversal_are_rejected_without_moving(self):
        path=self.take();token=self.store.page()['items'][0]['token'];(path/'meta.yaml').write_text((path/'meta.yaml').read_text()+'note: changed\n')
        with self.assertRaises(ValueError):self.store.delete(path.name,token)
        self.assertTrue(path.exists())
        for name in ('../outside','/etc/passwd','..'):
            with self.assertRaises(ValueError):self.store.delete(name,token)
    def test_delete_older_take_does_not_release_newer_successful_owner(self):
        old=self.take();new=self.take('L2-001-2026-10-30-10-01-00');row=next(v for v in self.store.page()['items'] if v['id']==old.name)
        result=self.store.delete(old.name,row['token']);self.assertFalse(result['reference_released']);self.assertEqual(ledger.read(self.library/'used.yaml')['used'][self.image.name]['session'],str(new))
    def test_delete_current_owner_with_older_retained_take_is_refused_before_journal(self):
        old=self.take();new=self.take('L2-001-2026-10-30-10-01-00');row=next(v for v in self.store.page()['items'] if v['id']==new.name)
        before=ledger.read(self.library/'used.yaml')
        with self.assertRaisesRegex(ValueError,'其他已保存记录'):self.store.delete(new.name,row['token'])
        self.assertTrue(old.exists());self.assertTrue(new.exists());self.assertEqual(ledger.read(self.library/'used.yaml'),before)
        self.assertEqual(list(self.store.operations.glob('*.json')),[])
        self.assertFalse((self.volume/'deleted-recordings').exists())
    def test_delete_recovers_after_rename_before_ledger_update(self):
        path=self.take();token=self.store.page()['items'][0]['token']
        with patch.object(ledger,'_release_locked',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):self.store.delete(path.name,token)
        self.assertFalse(path.exists());self.store.recover();self.assertEqual(ledger.read(self.library/'used.yaml')['used'],{})
        journal=json.loads(next(self.store.operations.glob('*.json')).read_text());self.assertEqual(journal['phase'],'complete')
    def test_delete_replays_after_ledger_release_before_task_progress(self):
        path=self.take();token=self.store.page()['items'][0]['token']
        progress=self.base/'runtime/reference_task_blocks.json'
        progress.write_text(json.dumps({'version':4,'current_task':'task','target_size':5,'completed_ids':[],'finished_tasks':[],'pending':None,'selected_ids':['img'],'finished_images':{},'image_refs':{'img':{'task':'task','relative_path':'configs/environments/scene/task_1.png'}},'saved_ids':['img'],'saved_history':['img'],'block_saved_start':None}))
        with patch('task_delete_restore._durable',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):self.store.delete(path.name,token)
        stamp=ledger.read(self.library/'used.yaml')['attempts'][self.image.name][0]['deleted_at']
        self.store.recover();self.store.recover()
        self.assertEqual(stamp,ledger.read(self.library/'used.yaml')['attempts'][self.image.name][0]['deleted_at'])
        self.assertEqual(json.loads(progress.read_text())['saved_history'],[])
        journal=json.loads(next(self.store.operations.glob('*.json')).read_text());self.assertEqual(journal['reference_restoration']['status'],'restored')
    def test_abandoned_preparation_is_outside_upload_tree_and_started_take_is_refused(self):
        path=self.take(status='cancelled',keep=False);(path/'start_gate.json').unlink();result=self.store.abandon_preparation(path)
        self.assertFalse(path.exists());self.assertTrue(result['deleted']);self.assertEqual(list((self.volume/'.discarding-recordings').iterdir()),[]);self.assertEqual(self.store.page('all')['total'],0)
        path=self.take();(path/'start_gate.json').write_text('{}')
        with self.assertRaises(ValueError):self.store.abandon_preparation(path)
    def test_date_range_filters_use_capture_day_and_exclude_archive(self):
        path=self.take();archive=self.volume/'archives/old';archive.mkdir(parents=True);os.rename(path,archive/path.name)
        self.assertEqual(self.store.page('all')['total'],0)
        with self.assertRaises(ValueError):self.store.page('yesterday-ish')
    @unittest.skipIf(os.name=='nt','POSIX directory-link safety')
    def test_linked_session_is_not_deletable(self):
        path=self.take();link=self.sessions/'linked';link.symlink_to(path)
        with self.assertRaises(ValueError):self.store.path('linked')

class AbandonTests(unittest.TestCase):
    def control(self,state='ready'):
        control=WebControls();control.state=state;control.worker=None;control.path=None;control.image=Path('reference.png');control.selected_devices=['cam_ros_left'];control.dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':False}))
        control.worker_status=Mock(return_value={'recording_active':False});control.stop_preview=Mock();control.close_emg_pairing=Mock();control.reference_release_pin=Mock();control.reference_refresh=Mock();control.scan_devices=Mock();control.state_write=Mock();control._reference_active=None;control._reference_retry={'old':True}
        return control
    def test_ready_exit_resets_reference_and_returns_device_confirmation(self):
        control=self.control();control.abandon_preparation();self.assertEqual(control.state,'device_confirm');self.assertIsNone(control.image);self.assertIsNone(control.path);self.assertIsNone(control.selected_devices);self.assertIsNone(control._reference_retry);control.close_emg_pairing.assert_called_once()
    def test_recording_countdown_and_stopped_result_cannot_be_abandoned_as_preparation(self):
        for state in ('recording','countdown','confirm_stop','finalizing'):
            with self.subTest(state=state),self.assertRaises(ValueError):self.control(state).abandon_preparation()
    def test_started_or_privacy_locked_worker_is_not_stopped_by_abandon(self):
        control=self.control();control.dashboard.privacy.snapshot=lambda:{'required':True}
        with self.assertRaises(ValueError):control.abandon_preparation()
        control=self.control();control.worker=Mock();control.worker_status.return_value={'recording_active':True}
        with self.assertRaises(ValueError):control.abandon_preparation()
        control.worker.wait.assert_not_called()
    def test_unstarted_worker_receives_stop_before_pin_is_released(self):
        control=self.control();control.worker=Mock();control.command=Mock();control.reap=Mock()
        control.abandon_preparation();control.command.assert_called_once_with('STOP');control.worker.wait.assert_called_once_with(timeout=50);control.reap.assert_called_once()
    def test_actions_expose_abandon_before_capture_and_not_during_capture(self):
        for state in ('ready','preparing','idle','device_confirm','emg_pairing','wrist_manual'):self.assertIn('abandon',dashboard.actions_for(state))
        for state in ('countdown','recording','confirm_stop','finalizing'):self.assertNotIn('abandon',dashboard.actions_for(state))

if __name__=='__main__':unittest.main()
