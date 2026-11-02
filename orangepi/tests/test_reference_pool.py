"""Mixed task pools exclude hidden data and freeze each task's own context."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src/orangepi_collect'/name) for name in ('control','common')]
from reference_catalog import ReferenceCatalog, CatalogError
from PIL import Image


class MixedPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name);self.base=self.project/'orangepi';self.base.mkdir()
        self.catalog=ReferenceCatalog(self.base,project=self.project)
        self.a=self.catalog.create_list('任务甲',self.catalog.revision)['id']
        self.b=self.catalog.create_list('任务乙',self.catalog.revision)['id']
        self.one=self.upload(self.a,'red','甲.png');self.two=self.upload(self.b,'blue','乙.png')

    def upload(self,target,color,name):
        data=io.BytesIO();Image.new('RGB',(16,12),color).save(data,'PNG')
        return self.catalog.upload(data.getvalue(),name,target,self.catalog.revision)['id']

    def setting(self,identifier,hidden=False,enabled=True,tags=None):
        return self.catalog.set_list_sampling(identifier,hidden,enabled,tags or [],self.catalog.revision)

    def freeze(self):return self.catalog.freeze_list('pool',self.catalog.revision)

    def test_pool_contains_both_lists_and_freezes_source_labels(self):
        self.setting(self.b,tags=['装配','工具'])
        snapshot=self.freeze();self.assertEqual({row['id'] for row in snapshot['items']},{self.one,self.two})
        index=next(i for i,row in enumerate(snapshot['items']) if row['id']==self.two)
        selection=self.catalog.per_item_selection(snapshot,index)
        self.assertEqual(selection['list_id'],'pool');self.assertEqual(selection['asset']['source_lists'],[{'id':self.b,'name':'任务乙'}])
        self.assertEqual(selection['asset']['tags'],['任务乙','装配','工具'])

    def test_hidden_lists_remain_viewable_but_all_sampling_paths_exclude_them(self):
        self.setting(self.a,hidden=True)
        self.assertEqual([row['id'] for row in self.freeze()['items']],[self.two])
        self.assertEqual(self.catalog.list_page(self.a)['items'][0]['id'],self.one)
        self.assertNotIn(self.a,[row['id'] for row in self.catalog.library_page()['lists']])
        self.assertEqual([row['id'] for row in self.catalog.library_page(hidden_only=True)['lists']],[self.a])
        self.assertNotIn(self.one,[row['id'] for row in self.catalog.freeze_list('all',self.catalog.revision)['items']])
        with self.assertRaises(CatalogError):self.catalog.freeze_list(self.a,self.catalog.revision)

    def test_restoring_is_explicit_and_does_not_automatically_reenable_sampling(self):
        self.setting(self.a,hidden=True);self.setting(self.a,enabled=False)
        self.assertEqual(self.freeze()['total'],1)
        self.setting(self.a);self.assertEqual(self.freeze()['total'],2)

    def test_duplicate_assets_are_drawn_once_and_keep_all_source_labels(self):
        duplicate=self.upload(self.b,'red','same.png');self.assertEqual(duplicate,self.one)
        snapshot=self.freeze();self.assertEqual(snapshot['total'],2)
        row=next(row for row in snapshot['items'] if row['id']==self.one)
        self.assertEqual({source['id'] for source in row['source_lists']},{self.a,self.b})

    def test_per_task_scene_and_filename_survive_pool_snapshot_and_later_catalog_edit(self):
        self.catalog.set_list_scene(self.a,'甲场景',None,{'config.yaml':b'a: 1\n'},self.catalog.revision)
        self.catalog.set_list_scene(self.b,'乙场景',None,{'config.yaml':b'b: 2\n'},self.catalog.revision)
        snapshot=self.freeze();index=next(i for i,row in enumerate(snapshot['items']) if row['id']==self.two)
        self.catalog.set_list_scene(self.b,'乙场景更新',None,{'other.yaml':b'b: 3\n'},self.catalog.revision)
        output=self.project/'episode';output.mkdir()
        scene=self.catalog.snapshot_scene(snapshot,output,index)
        self.assertEqual(scene['task'],'乙场景');self.assertEqual((output/'reference_bundle/config.yaml').read_bytes(),b'b: 2\n')
        self.assertNotIn('_scene_id',self.catalog.per_item_selection(snapshot,index)['asset'])

    def test_incomplete_bundle_and_completed_asset_are_excluded(self):
        self.catalog.begin_bundle('未导入完成',[dict(name='new.png',size=10,crc=0,kind='image')],self.catalog.revision)
        snapshot=self.freeze();completed=[snapshot['items'][0]['relative_path']]
        self.assertEqual(self.catalog.freeze_list('pool',self.catalog.revision,completed_paths=completed)['total'],1)

    def test_sampling_changes_preserve_pin_and_require_current_revision(self):
        revision=self.catalog.revision;self.setting(self.b,enabled=False)
        with self.assertRaises(CatalogError):self.catalog.set_list_sampling(self.a,True,False,[],revision)
        self.catalog.pin(self.one,'active_episode')
        with self.assertRaises(CatalogError):self.setting(self.a,hidden=True)
        self.assertEqual(self.freeze()['total'],1)

if __name__=='__main__':unittest.main()
