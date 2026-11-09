"""Stop/keep/discard protocol and scoped, recoverable file removal."""
import io,json,queue,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import framework_recorder as fr
import launcher,recording_store
from test_recording_lifecycle import StoreTests


class InputQueue:
    def __init__(self):self.queue=queue.Queue()
    def __iter__(self):return self
    def __next__(self):
        value=self.queue.get()
        if value is None:raise StopIteration
        return value+'\n'


class Native:
    def __init__(self,root):self.root=root;self.returncode=None;self.stdin=Mock();self.stdin.write.side_effect=self.write
    def poll(self):return self.returncode
    def write(self,line):
        if line.strip()=='STOP':
            (self.root/'capture_summary.json').write_text(json.dumps({'status':'success','errors':[],'emg_right':{'samples':500}}))
            self.returncode=0


class RecorderDecisionTests(unittest.TestCase):
    def run_protocol(self,decision,assert_wait=True):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'meta.yaml').write_text('{}')
            rec=fr.PiBundleRecorder.__new__(fr.PiBundleRecorder)
            rec.config=SimpleNamespace(session_dir=str(root),stdin_control=True,wait_for_save=True,duration=0)
            rec.stop_event=threading.Event();rec._commit_evt=threading.Event();rec.process=Native(root)
            incoming=InputQueue();errors=[]
            def record():
                try:rec._record()
                except BaseException as exc:errors.append(exc)
            summary={'status':'success','errors':[],'emg_right':{'samples':475}}
            report=SimpleNamespace(level='INFO')
            with patch.object(fr.sys,'stdin',incoming),patch.object(fr,'standardize',return_value=summary) as standardize, \
                 patch.object(fr,'check_session',return_value=report) as qc, \
                 patch.object(fr,'performance',return_value={'qc_cpu_ids':list(__import__('os').sched_getaffinity(0)),'qc_address_space_mb':2048}), \
                 patch.object(fr.os,'sched_setaffinity'),patch.object(fr.os,'nice'), \
                 patch('resource.setrlimit'),patch('orangepi_collect.storage.durability.sync_session',return_value={'completed':True}):
                thread=threading.Thread(target=record);thread.start()
                try:
                    incoming.queue.put('START');incoming.queue.put('STOP')
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        try:
                            if json.loads((root/'status.json').read_text())['state']=='awaiting_decision':break
                        except (OSError,ValueError):pass
                        time.sleep(.005)
                    self.assertTrue(thread.is_alive(),'adapter must wait for operator choice')
                    standardize.assert_not_called();qc.assert_not_called()
                    if decision=='DURABLE_DISCARD':
                        (root/'discard_request.json').write_text(json.dumps({'session_id':root.name,'directory':str(root.resolve())}))
                    elif decision=='EOF':incoming.queue.put(None)
                    else:incoming.queue.put(decision)
                    thread.join(3)
                    self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
                    actual=json.loads((root/'capture_summary.json').read_text())
                    if decision=='KEEP':
                        standardize.assert_called_once();qc.assert_called_once();self.assertEqual(actual['emg_right']['samples'],475)
                        self.assertEqual(actual['status'],'success')
                    else:
                        standardize.assert_not_called();qc.assert_not_called();self.assertTrue(actual['qc_skipped'])
                        self.assertEqual(actual['status'],'cancelled' if decision in ('EOF','CANCEL') else 'discarded')
                finally:
                    incoming.queue.put(None);rec.stop_event.set();thread.join(3)

    def test_stop_waits_and_keep_runs_qc_only_after_choice(self):self.run_protocol('KEEP')
    def test_stop_then_discard_never_runs_qc(self):self.run_protocol('DISCARD')
    def test_durable_discard_is_seen_without_pipe_command(self):self.run_protocol('DURABLE_DISCARD')
    def test_parent_pipe_close_preserves_cancelled_data_without_qc(self):self.run_protocol('EOF')
    def test_shutdown_cancel_never_runs_qc(self):self.run_protocol('CANCEL')


class DiscardStoreTests(unittest.TestCase):
    setUp=StoreTests.setUp
    take=StoreTests.take
    def prepared(self,name='L2-001-2026-10-30-10-00-00'):
        path=self.take(name,status='cancelled',keep=False)
        (path/'discard_request.json').write_text(json.dumps({'session_id':path.name,'directory':str(path.resolve())}))
        (path/'stop_gate.json').write_text('{"recording_active":false}')
        return path

    def test_discard_removes_bytes_without_touching_other_takes(self):
        source=self.prepared();other=self.take('other-retained')
        result=self.store.discard_current(source)
        self.assertTrue(result['deleted']);self.assertFalse(source.exists());self.assertTrue(other.is_dir())
        self.assertEqual((other/'frames.mp4').read_bytes(),b'preserved-video')
        self.assertEqual(list((self.volume/'.discarding-recordings').iterdir()),[])
        self.assertFalse((self.volume/'deleted-recordings').exists())

    def test_retained_take_cannot_be_purged_with_discard_request(self):
        source=self.take();(source/'discard_request.json').write_text(json.dumps({'session_id':source.name,'directory':str(source.resolve())}))
        (source/'stop_gate.json').write_text('{"recording_active":false}')
        with self.assertRaisesRegex(ValueError,'已保留'):self.store.discard_current(source)
        self.assertTrue((source/'frames.mp4').exists())

    def test_mismatched_request_and_active_gate_are_rejected(self):
        source=self.prepared();request=source/'discard_request.json';saved=request.read_bytes();request.write_text('{}')
        with self.assertRaises(ValueError):self.store.discard_current(source)
        request.write_bytes(saved);(source/'stop_gate.json').write_text('{"recording_active":true}')
        with self.assertRaises(ValueError):self.store.discard_current(source)
        self.assertTrue(source.is_dir())

    def test_external_path_with_same_basename_is_rejected(self):
        source=self.prepared();external=self.root/source.name;external.mkdir()
        with self.assertRaises(ValueError):self.store.discard_current(external)
        self.assertTrue(source.is_dir());self.assertTrue(external.is_dir())

    def test_interrupted_purge_is_recovered_from_external_journal(self):
        source=self.prepared()
        with patch.object(recording_store.shutil,'rmtree',side_effect=OSError('simulated disk interruption')):
            with self.assertRaises(OSError):self.store.discard_current(source)
        self.assertFalse(source.exists());self.assertEqual(len(list((self.volume/'.discarding-recordings').iterdir())),1)
        self.store.recover()
        self.assertEqual(list((self.volume/'.discarding-recordings').iterdir()),[])
        self.assertTrue(all(json.loads(p.read_text())['phase']=='complete' for p in self.store.operations.glob('*.json')))

    def test_link_inside_current_take_cannot_touch_external_file(self):
        source=self.prepared();external=self.root/'outside.bin';external.write_bytes(b'outside')
        (source/'linked').symlink_to(external)
        with self.assertRaises(ValueError):self.store.discard_current(source)
        self.assertEqual(external.read_bytes(),b'outside');self.assertTrue(source.is_dir())


class ControllerDiscardTests(unittest.TestCase):
    def test_retry_reaps_and_deletes_before_preparing_same_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);old=c.path
            c.state='confirm_stop';c.dashboard=SimpleNamespace(privacy=SimpleNamespace(snapshot=lambda:{'required':False}))
            c.worker=Mock();c.worker.poll.return_value=0;c.command=Mock();c.state_write=Mock();c.voice=Mock();c.quit=False
            c.recording_store=Mock();c.last_review_path=None;c.reference_refresh=Mock();c.prepare=Mock()
            events=[]
            c.reap=lambda:(events.append('reap'),setattr(c,'worker',None))
            c.recording_store.discard_current.side_effect=lambda path:events.append('delete')
            c.prepare.side_effect=lambda:events.append('prepare')
            c.discard_and_retry()
            self.assertEqual(events,['reap','delete','prepare']);c.recording_store.discard_current.assert_called_once_with(old)
            self.assertIsNone(c.path);self.assertEqual(c._reference_reap_outcome,'discarded')
            self.assertEqual(json.loads((old/'discard_request.json').read_text())['session_id'],old.name)

    def test_retry_does_not_delete_or_prepare_while_owner_is_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            c=launcher.Controller.__new__(launcher.Controller);c.path=Path(temp);c.state='confirm_stop'
            c.worker=Mock();c.worker.poll.return_value=None;c.command=Mock();c.state_write=Mock();c.voice=Mock()
            c.recording_store=Mock();c.prepare=Mock()
            c.discard_and_retry()
            c.command.assert_called_once_with('DISCARD');self.assertEqual(c.state,'discarding')
            c.recording_store.discard_current.assert_not_called();c.prepare.assert_not_called()

    def test_keep_explicitly_unblocks_quality_check(self):
        c=launcher.Controller.__new__(launcher.Controller);c.worker=Mock();c.worker.poll.return_value=None
        c.command=Mock();c.state_write=Mock();c.voice=Mock();c.finish()
        c.command.assert_called_once_with('KEEP');self.assertTrue(c.keep_requested);self.assertEqual(c.state,'finalizing')


if __name__=='__main__':unittest.main()
