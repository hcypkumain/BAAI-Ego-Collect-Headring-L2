import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image
from reference_catalog import ReferenceCatalog
from reference_controls import ReferenceControlsMixin
from task_block_sampling import TaskBlocks

class Controller(ReferenceControlsMixin):pass
class ManualReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.project=Path(self.tmp.name);base=self.project/'orangepi';(base/'configs').mkdir(parents=True)
        cat=ReferenceCatalog(base,project=self.project);ids=[]
        for n in range(2):
            lid=cat.create_list('任务'+str(n),cat.revision)['id'];ids.append(lid)
            for i in range(3):
                raw=io.BytesIO();Image.new('RGB',(16,16),(n*100,i*60,50)).save(raw,'PNG')
                cat.upload(raw.getvalue(),'任务%d_%d.png'%(n,i),lid,cat.revision)
        settings=dict(version=1,list_ids=ids,block_size=5)
        (base/'configs/task_block_policy.json').write_text(json.dumps(settings))
        self.cat=cat=ReferenceCatalog(base,project=self.project);self.ids=ids
        c=self.c=Controller();c.reference_catalog=cat;c._reference_base=base;c._reference_project=self.project
        c._reference_preferences={'selected_list_id':'pool'};c._reference_preferences_error=None;c.reference_error=None
        c._reference_retry=None;c._reference_active=None;c._reference_snapshot=None;c._reference_completed=()
        c._reference_cli_image=None;c._reference_cli_force=False;c._reference_manual_id=None;c._reference_histories={}
        c._deferred_catalog_revision=None;c._reference_reap_outcome=None;c._reference_task_blocks=TaskBlocks(base,settings)
        c.state='device_confirm';c.worker=None;c.path=None;c.image=None;c.quit=False;c.selected_devices=None
        c.state_write=Mock();c.prepare=Mock();c.release_prepared=Mock();c.refresh_reference_histories=Mock()
        c._apply_reference_snapshot(cat.freeze_list('pool',cat.revision,include_completed=True))
    def select(self,n):self.c.select_reference_list({'list_id':self.ids[n],'catalog_revision':self.cat.revision})
    def prepared(self):
        self.select(0);c=self.c;c.reference_refresh=Mock();selection=c.reference_begin_preparation()
        path=self.project/'data'/'session-night'/'test-session';path.mkdir(parents=True)
        c.path=path;c._reference_active.update(path=path,identity={'session_id':'test-session'})
        meta=dict(session_id='test-session',reference_image=selection['asset']['relative_path'],reference_sha256=selection['asset']['sha256'],reference_selection=selection,selected_devices=['eeg'])
        c.reference_set_raw_metadata(meta);(path/'meta.json').write_text(json.dumps(meta));(path/'reference_queue.json').write_text(json.dumps(c._reference_active['entry']['snapshot']))
        c.state='ready';c.selected_devices=['eeg'];c.worker=Mock();c.worker.poll.return_value=None
        c.worker_status=Mock(return_value={'state':'ready','recording_active':False,'eeg_impedance':{'phase':'completed','request_id':'same-measurement'}})
        c.dashboard=Mock();c.dashboard.privacy.snapshot.return_value={'required':False,'stop_confirmed':True,'phase':'idle'}
        gate=patch('dashboard.impedance_allows_recording',return_value=True,create=True);gate.start();self.addCleanup(gate.stop)
        return c.worker
    def test_cross_task_switch_keeps_ready_owner_and_measurement(self):
        worker=self.prepared();path=self.c.path;self.select(1)
        self.assertIs(self.c.worker,worker);self.assertEqual(self.c.state,'ready');self.assertEqual(self.c.path,path)
        self.c.release_prepared.assert_not_called();worker.stdin.write.assert_not_called()
        self.assertEqual(self.c.worker_status()['eeg_impedance']['request_id'],'same-measurement')
        meta=json.loads((path/'meta.json').read_text());self.assertEqual(meta['reference_selection']['asset']['source_lists'][0]['id'],self.ids[1])
        self.assertEqual(meta['reference_image'],str(self.c.image.relative_to(self.project)))
        self.assertEqual(json.loads((path/'reference_queue.json').read_text())['queue_sha256'],meta['reference_selection']['queue_sha256'])
    def test_explicit_image_from_other_task_updates_metadata(self):
        self.prepared();snapshot=self.cat.freeze_list('pool',self.cat.revision,include_completed=True)
        image=next(x for x in snapshot['items'] if x['source_lists'][0]['id']==self.ids[1])
        self.c.select_reference_image({'image_id':image['id'],'list_id':self.ids[1],'catalog_revision':self.cat.revision})
        self.assertEqual(self.c.reference_image_state()['id'],image['id'])
        self.assertEqual(self.c._reference_task_blocks.value['current_task'],self.ids[1])
    def test_impedance_not_complete_blocks_selection(self):
        self.prepared()
        with patch('dashboard.impedance_allows_recording',return_value=False,create=True):
            self.assertFalse(self.c.reference_can_switch_list())
            with self.assertRaises(ValueError):self.select(1)
    def test_start_gate_blocks_even_if_worker_says_ready(self):
        self.prepared();(self.c.path/'start_gate.json').write_text('{}')
        with self.assertRaises(ValueError):self.select(1)
    def test_recording_and_privacy_block_switch(self):
        self.prepared()
        for state in ['recording','countdown','confirm_stop','finalizing']:
            self.c.state=state
            with self.assertRaises(ValueError):self.select(1)
        self.c.state='ready';self.c.dashboard.privacy.snapshot.return_value={'required':True,'stop_confirmed':False,'phase':'arming'}
        with self.assertRaises(ValueError):self.select(1)
    def test_stale_revision_preserves_preparation(self):
        worker=self.prepared();meta=(self.c.path/'meta.json').read_bytes();before=copy.deepcopy(self.c._reference_task_blocks.value)
        with self.assertRaises(ValueError):self.c.select_reference_list({'list_id':self.ids[1],'catalog_revision':self.cat.revision-1})
        self.assertEqual((self.c.path/'meta.json').read_bytes(),meta);self.assertEqual(self.c._reference_task_blocks.value,before);self.assertIs(self.c.worker,worker)
    def test_bundle_failure_rolls_back_without_losing_old_bundle(self):
        self.prepared();bundle=self.c.path/'reference_bundle';bundle.mkdir();(bundle/'old.txt').write_text('old')
        before=(self.c.path/'meta.json').read_bytes();old=self.c._reference_active
        with patch.object(self.cat,'snapshot_scene',side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):self.select(1)
        self.assertTrue((bundle/'old.txt').exists());self.assertEqual((self.c.path/'meta.json').read_bytes(),before);self.assertIs(self.c._reference_active,old)
    def test_five_image_mode_allows_manual_task_change(self):
        self.select(0);self.select(1)
        self.assertEqual(self.c._reference_task_blocks.value['current_task'],self.ids[1]);self.assertTrue(self.c.reference_task_state()['manual_task_switch'])

class ManualReferenceAllModeTests(ManualReferenceTests):
    def setUp(self):
        super().setUp();self.c._reference_task_blocks.size='all'

if __name__=='__main__':unittest.main(verbosity=2)
