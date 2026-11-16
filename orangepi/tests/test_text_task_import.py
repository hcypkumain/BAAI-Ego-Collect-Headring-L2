import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control'),str(ROOT/'storage'),str(ROOT/'framework')]
from reference_catalog import ReferenceCatalog
from text_task_import import append,retire
from text_reference import read
import ledger

class TextImportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.project=Path(self.tmp.name);self.base=self.project/'orangepi';(self.base/'runtime').mkdir(parents=True);(self.base/'configs').mkdir();self.library=self.project/'configs/environments';self.library.mkdir(parents=True)
  self.catalog=ReferenceCatalog(self.base,project=self.project)
  legacy=self.library/'old_1.png';legacy.write_bytes(b'existing picture');self.catalog.import_legacy([legacy]);self.original=copy.deepcopy(self.catalog._load());self.legacy=legacy
  tasks=[{'id':'text_%032x'%i,'scene':'洗衣区','name':('任务%d'%i)+('\n' if i==0 else ''),'reference_text':'第一步\n第二步 <安全>','target_count':50} for i in range(20)]
  self.manifest={'schema_version':1,'namespace':'temporary-test-fixture','source_sha256':'a'*64,'temporary':True,'task_count':20,'target_per_task':50,'total_target':1000,'tasks':tasks};self.raw=json.dumps(self.manifest,ensure_ascii=False).encode()
 def test_append_preserves_old_pool_and_is_idempotent(self):
  old_pool=self.catalog.freeze_list('pool',self.catalog.revision,include_completed=True);res=append(self.catalog,self.raw,self.catalog.revision);self.assertEqual((res['tasks'],res['occurrences']),(20,1000))
  now=self.catalog._load()
  for key,item in self.original['images'].items():self.assertEqual(now['images'][key],item)
  for key,item in self.original['lists'].items():self.assertEqual(now['lists'][key],item)
  self.assertEqual(self.legacy.read_bytes(),b'existing picture');pool=self.catalog.freeze_list('pool',self.catalog.revision,include_completed=True);self.assertEqual([x['id'] for x in pool['items']],[x['id'] for x in old_pool['items']])
  text=self.catalog.freeze_list(res['pool_id'],self.catalog.revision,include_completed=True);self.assertEqual(text['total'],1000);self.assertEqual(len({x['source_lists'][0]['id'] for x in text['items']}),20)
  child=now['lists'][res['pool_id']]['text_task_list_ids'][0];self.assertTrue(now['lists'][child]['hidden'])
  with self.assertRaises(ValueError):self.catalog.freeze_list(child,self.catalog.revision,include_completed=True)
  still=self.catalog.freeze_list('pool',self.catalog.revision,include_completed=True);self.assertEqual([x['id'] for x in still['items']],[x['id'] for x in old_pool['items']])
  with self.catalog._transaction(self.catalog.revision) as v:v['lists'][child]['hidden']=False;v['lists'][child]['sampling_enabled']=True
  with self.assertRaises(ValueError):self.catalog.freeze_list(child,self.catalog.revision,include_completed=True)
  still=self.catalog.freeze_list('pool',self.catalog.revision,include_completed=True);self.assertEqual([x['id'] for x in still['items']],[x['id'] for x in old_pool['items']])
  with self.catalog._transaction(self.catalog.revision) as v:v['lists'][child]['hidden']=True;v['lists'][child]['sampling_enabled']=False
  text=self.catalog.freeze_list(res['pool_id'],self.catalog.revision,include_completed=True)
  selected=self.catalog.per_item_selection(text,0);self.assertEqual(selected['asset']['kind'],'text');path=self.project/selected['asset']['relative_path'];item=read(path.read_bytes());self.assertEqual(item['reference_text'],'第一步\n第二步 <安全>');self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),selected['asset']['sha256'])
  again=append(self.catalog,self.raw,self.catalog.revision);self.assertTrue(again['already']);self.assertEqual(again['revision'],res['revision']+2)
 def test_retire_only_hides_temporary_lists_and_keeps_evidence(self):
  res=append(self.catalog,self.raw,self.catalog.revision);snapshot=self.catalog.freeze_list(res['pool_id'],self.catalog.revision,include_completed=True);path=self.project/snapshot['items'][0]['relative_path'];raw=path.read_bytes()
  result=retire(self.catalog,self.manifest['namespace'],self.catalog.revision);self.assertEqual(result['lists'],21);self.assertEqual(path.read_bytes(),raw)
  now=self.catalog._load()
  for key,item in self.original['lists'].items():self.assertEqual(now['lists'][key],item)
  for key,item in self.original['images'].items():self.assertEqual(now['images'][key],item)
  self.assertTrue(now['lists'][res['pool_id']]['deleted'])
 def test_expected_absent_refuses_existing_catalog_without_changes(self):
  before=copy.deepcopy(self.catalog._load())
  with self.assertRaises(ValueError):append(self.catalog,self.raw,self.catalog.revision,require_catalog_absent=True)
  self.assertEqual(self.catalog._load(),before)
 def test_cloned_text_list_cannot_bypass_round_pool(self):
  res=append(self.catalog,self.raw,self.catalog.revision);clone=self.catalog.clone_list(res['pool_id'],'复制文字任务',self.catalog.revision)
  with self.assertRaises(ValueError):self.catalog.freeze_list(clone['id'],self.catalog.revision,include_completed=True)
 def test_retired_text_pool_falls_back_without_erasing_round_state(self):
  import reference_controls
  from reference_catalog import CatalogError
  from unittest.mock import Mock
  class Owner(reference_controls.ReferenceControlsMixin):
   pass
  o=Owner();o._reference_base=self.base;o._reference_project=self.project;o._reference_ledger=ledger;o._reference_snapshot={'catalog_revision':0};o._reference_retry=None;o._reference_cli_image=None;o._reference_preferences={'version':1,'collector_id':'L2-017-EEG','selected_list_id':'textpool'};o.reference_catalog=Mock();o.reference_catalog.revision=2;o.reference_catalog.freeze_list.side_effect=[CatalogError('hidden','hidden_list',409),{'list_id':'pool','catalog_revision':2,'items':[],'total':0}];o.reference_catalog.single_list_mode=False;o.reference_catalog._load.return_value={'lists':{}};o._base_reference_task_blocks=None;o._reference_task_blocks=Mock(round_robin=True);o.refresh_reference_histories=Mock();o._apply_reference_snapshot=Mock(side_effect=lambda snapshot:self.assertIsNone(o._reference_task_blocks));o._reference_preferences_error=None
  with patch.object(reference_controls,'_save_preferences'),patch.object(reference_controls,'_completed_paths',return_value=()),patch.object(reference_controls,'_reserved_paths',return_value=()):o.reference_refresh(force=True)
  self.assertEqual(o._reference_preferences['selected_list_id'],'pool')
 def test_eeg_pool_selection_is_scoped_and_can_exit(self):
  import reference_controls,project_paths
  res=append(self.catalog,self.raw,self.catalog.revision)
  class Owner(reference_controls.ReferenceControlsMixin):
   def __init__(o):
    o.reference_catalog=self.catalog;o._reference_base=self.base;o._reference_project=self.project;o._reference_ledger=ledger
    o._reference_preferences={'version':1,'collector_id':'L2-017-EEG','selected_list_id':'all'};o._base_reference_task_blocks=None;o._reference_task_blocks=None;o.state='device_confirm';o.worker=None;o.quit=False;o._reference_retry=None;o._reference_manual_id=None;o.images=[]
   def _apply_reference_snapshot(o,snapshot):o._reference_snapshot=snapshot;o.images=[x['relative_path'] for x in snapshot['items']]
   def state_write(o):pass
  o=Owner()
  with patch.object(project_paths,'COLLECTION_MODE','eeg'),patch.object(reference_controls,'_save_preferences'),patch.object(reference_controls,'_completed_paths',return_value=()),patch.object(reference_controls,'_reserved_paths',return_value=()):
   o.select_reference_list({'list_id':res['pool_id'],'catalog_revision':self.catalog.revision});self.assertTrue(o._reference_task_blocks.round_robin);self.assertEqual(o._reference_snapshot['total'],1000)
  with patch.object(project_paths,'COLLECTION_MODE','eeg'),patch.object(reference_controls,'_save_preferences'),patch.object(reference_controls,'_completed_paths',return_value=()),patch.object(reference_controls,'_reserved_paths',return_value=()):
   oldid=next(iter(self.original['lists']));o.select_reference_list({'list_id':oldid,'catalog_revision':self.catalog.revision})
   self.assertFalse(o._reference_task_blocks.round_robin);self.assertEqual(o._reference_task_blocks.count_basis,'saved')
   self.assertEqual(o._reference_task_blocks.size,5)
   o.select_reference_list({'list_id':res['pool_id'],'catalog_revision':self.catalog.revision})
   self.assertTrue(o._reference_task_blocks.round_robin);self.assertEqual(o._reference_task_blocks.count_basis,'saved')
  o=Owner()
  with patch.object(project_paths,'COLLECTION_MODE','eeg'),patch.object(reference_controls,'_save_preferences',side_effect=OSError('fail')):
   with self.assertRaises(OSError):o.select_reference_list({'list_id':res['pool_id'],'catalog_revision':self.catalog.revision})
   self.assertIsNone(o._reference_task_blocks);self.assertEqual(o._reference_preferences['selected_list_id'],'all')
 def test_one_success_marks_only_one_occurrence(self):
  res=append(self.catalog,self.raw,self.catalog.revision);snapshot=self.catalog.freeze_list(res['pool_id'],self.catalog.revision,include_completed=True);asset=snapshot['items'][0];path=self.project/asset['relative_path'];other=self.project/snapshot['items'][1]['relative_path'];session=self.project/'session';session.mkdir()
  with patch.object(ledger,'LIBRARY',self.library),patch.object(ledger,'PROJECT',self.project),patch.object(ledger,'BASE',self.base),patch.object(ledger,'_qualified_session',return_value=True):
   self.assertTrue(ledger.mark(path,session,'success'));entries=ledger.read(path.parent/'used.yaml')['used'];self.assertIn(path.name,entries);self.assertNotIn(other.name,entries)
 def test_frozen_qc_evidence_requires_exact_text(self):
  import framework_qc
  res=append(self.catalog,self.raw,self.catalog.revision);snapshot=self.catalog.freeze_list(res['pool_id'],self.catalog.revision,include_completed=True);selection=self.catalog.per_item_selection(snapshot,0);path=self.project/selection['asset']['relative_path'];payload=read(path.read_bytes())
  meta={'reference_image':selection['asset']['relative_path'],'reference_sha256':selection['asset']['sha256'],'reference_selection':selection,'reference_kind':'text','reference_text':payload['reference_text'],'reference_text_payload':payload}
  self.assertEqual(framework_qc._frozen_reference_selection(meta,snapshot)['asset']['id'],selection['asset']['id'])
  tampered=copy.deepcopy(meta);tampered['reference_text_payload']['reference_text']='changed';tampered['reference_text']='changed'
  with self.assertRaises(ValueError):framework_qc._frozen_reference_selection(tampered,snapshot)
if __name__=='__main__':unittest.main()
