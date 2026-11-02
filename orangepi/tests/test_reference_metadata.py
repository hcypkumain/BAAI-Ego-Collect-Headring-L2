"""Frozen pool tags remain compatible with existing per-episode QC proofs."""
import copy
from pathlib import Path
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
from framework_qc import _frozen_reference_selection

class ReferenceMetadataTests(unittest.TestCase):
    def metadata(self):
        asset=dict(id='image_abc',name='任务图',origin='upload',relative_path='configs/environments/_uploads/image_abc_1.png',sha256='a'*64,
                   tags=['工作台','工具'],source_lists=[dict(id='list_one',name='测量工件长度')])
        return dict(reference_image=asset['relative_path'],reference_sha256=asset['sha256'],reference_selection=dict(version=1,catalog_revision=2,
            list_id='pool',list_name='混合任务池',queue_sha256='b'*64,position=1,total=2,asset=asset))
    def test_pool_tags_and_source_lists_survive_validation(self):
        value=self.metadata();self.assertEqual(_frozen_reference_selection(value),value['reference_selection'])
    def test_older_episode_without_tags_remains_valid(self):
        value=self.metadata();asset=value['reference_selection']['asset'];asset.pop('tags');asset.pop('source_lists')
        self.assertEqual(_frozen_reference_selection(value),value['reference_selection'])
    def test_malformed_tags_or_sources_are_rejected(self):
        for key,bad in [('tags',['']),('tags','invalid'),('source_lists',[]),('source_lists',[dict(id='../bad',name='bad')])]:
            value=self.metadata();value['reference_selection']['asset'][key]=bad
            with self.assertRaises(ValueError):_frozen_reference_selection(value)
    def test_frozen_hash_still_must_match_actual_reference_proof(self):
        value=self.metadata();value['reference_sha256']='c'*64
        with self.assertRaises(ValueError):_frozen_reference_selection(value)

if __name__=='__main__':unittest.main()
