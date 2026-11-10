import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from PIL import Image

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src/orangepi_collect'/name) for name in ('control','common')]
from reference_catalog import ReferenceCatalog, _encoded, _durable
from usb_task_import import UsbTaskImporter, recover_replace
import hashlib


class UsbTaskImportMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name);self.base=self.project/'orangepi';self.base.mkdir()
        (self.base/'configs').mkdir()
        self.catalog=ReferenceCatalog(self.base,project=self.project)
        self.old_id=self.catalog.create_list('旧列表',self.catalog.revision)['id']
        self.image=self.png('red')
        self.old_image=self.catalog.upload(self.image,'原始名称',self.old_id,self.catalog.revision)['id']
        _durable(self.base/'configs/reference_control.json',_encoded(dict(version=1,collector_id='collector',selected_list_id=self.old_id)))
        self.usb=self.project/'usb';self.usb.mkdir()
        self.zip_path=self.usb/'new.zip'
        self.write_zip(self.zip_path, [('新名称.png',self.png('blue'))])
        self.importer=UsbTaskImporter(self.base,self.project,self.catalog,volumes=lambda:[dict(mountpoint=str(self.usb),label='USB')])

    @staticmethod
    def png(color):
        out=io.BytesIO();Image.new('RGB',(12,10),color).save(out,'PNG');return out.getvalue()

    @staticmethod
    def write_zip(path, images):
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,raw in images:archive.writestr(name,raw)

    def prepare(self, remove_ids=()):
        scanned=self.importer.scan();token=scanned['files'][0]['id']
        entry=self.importer.files[token]
        self.importer._build([entry],scanned['catalog_revision'],list(remove_ids))
        self.assertEqual(self.importer.status()['state'],'ready')

    def test_import_merges_and_preserves_existing_selection(self):
        self.prepare();self.importer.commit()
        state=self.catalog._load()
        self.assertIn(self.old_id,state['lists'])
        self.assertFalse(state['lists'][self.old_id]['deleted'])
        self.assertEqual(len([x for x in state['lists'].values() if not x['deleted']]),2)
        prefs=json.loads((self.base/'configs/reference_control.json').read_text())
        self.assertEqual(prefs['selected_list_id'],self.old_id)
        self.assertIn(self.old_image,state['images'])

    def test_hash_duplicate_keeps_existing_image_metadata(self):
        duplicate=self.png('red');self.write_zip(self.zip_path,[('renamed.png',duplicate)])
        self.prepare();self.importer.commit()
        state=self.catalog._load()
        self.assertEqual(len(state['images']),1)
        self.assertEqual(state['images'][self.old_image]['name'],'原始名称')

    def test_selected_old_list_is_soft_deleted_and_old_library_flag_is_unchanged(self):
        original=self.catalog._load();original['legacy_import_disabled']=False
        _durable(self.catalog.path,_encoded(original));self.catalog._cache=None;self.catalog._stamp=None
        self.prepare([self.old_id]);self.importer.commit()
        state=self.catalog._load()
        self.assertTrue(state['lists'][self.old_id]['deleted'])
        self.assertFalse(state['legacy_import_disabled'])
        self.assertIn(self.old_image,state['images'])

    def test_old_replace_request_is_rejected(self):
        scanned=self.importer.scan();token=scanned['files'][0]['id']
        with self.assertRaisesRegex(ValueError,'重新扫描'):
            self.importer.start(dict(scan_id=scanned['scan_id'],zip_ids=[token],catalog_revision=scanned['catalog_revision'],confirm_replace=True))

    def test_current_request_schema_runs_start_thread_and_commit(self):
        scanned=self.importer.scan();token=scanned['files'][0]['id']
        self.importer.start(dict(scan_id=scanned['scan_id'],zip_ids=[token],remove_list_ids=[],catalog_revision=scanned['catalog_revision']))
        self.importer.thread.join(5)
        self.assertEqual(self.importer.status()['state'],'ready')
        self.importer.commit()
        self.assertEqual(len([x for x in self.catalog._load()['lists'].values() if not x['deleted']]),2)

    def test_delete_only_request_soft_deletes_without_importing(self):
        scanned=self.importer.scan()
        self.importer.start(dict(scan_id=scanned['scan_id'],zip_ids=[],remove_list_ids=[self.old_id],catalog_revision=scanned['catalog_revision']))
        self.importer.thread.join(5)
        self.assertEqual(self.importer.status()['state'],'ready')
        self.importer.commit()
        self.assertTrue(self.catalog._load()['lists'][self.old_id]['deleted'])
        self.assertIn(self.old_image,self.catalog._load()['images'])

    def test_pool_selection_is_preserved(self):
        preferences=dict(version=1,collector_id='collector',selected_list_id='pool')
        _durable(self.base/'configs/reference_control.json',_encoded(preferences))
        self.prepare();self.importer.commit()
        self.assertEqual(json.loads((self.base/'configs/reference_control.json').read_text())['selected_list_id'],'pool')

    def test_readonly_list_and_its_copy_cannot_be_deleted_together(self):
        state=self.catalog._load()
        state['lists'][self.old_id]['readonly']=True
        _durable(self.catalog.path,_encoded(state));self.catalog._cache=None;self.catalog._stamp=None
        self.catalog._load()
        copied=self.catalog.clone_list(self.old_id,'副本',self.catalog.revision)['id']
        scanned=self.importer.scan();entry=self.importer.files[scanned['files'][0]['id']]
        self.importer._build([entry],scanned['catalog_revision'],[self.old_id,copied])
        self.assertEqual(self.importer.status()['state'],'failed')
        self.assertFalse(self.catalog._load()['lists'][self.old_id]['deleted'])
        self.assertFalse(self.catalog._load()['lists'][copied]['deleted'])

    def test_scan_revision_change_prevents_start(self):
        scanned=self.importer.scan();token=scanned['files'][0]['id']
        self.catalog.create_list('并发新列表',self.catalog.revision)
        with self.assertRaisesRegex(ValueError,'图库已更新'):
            self.importer.start(dict(scan_id=scanned['scan_id'],zip_ids=[token],remove_list_ids=[],catalog_revision=scanned['catalog_revision']))

    def test_invalid_zip_fails_without_changing_catalog(self):
        before=self.catalog.path.read_bytes();self.write_zip(self.zip_path,[('ignored.png',b'not a png')])
        scanned=self.importer.scan();entry=self.importer.files[scanned['files'][0]['id']]
        self.importer._build([entry],scanned['catalog_revision'],[])
        self.assertEqual(self.importer.status()['state'],'failed')
        self.assertEqual(self.catalog.path.read_bytes(),before)

    def test_pending_journal_recovers_catalog_preferences(self):
        old=self.catalog._load();old_raw=_encoded(old);old_prefs=json.loads((self.base/'configs/reference_control.json').read_text())
        new_prefs=dict(old_prefs,selected_list_id='pool')
        journal=self.base/'runtime/usb_task_import/pending.json';journal.parent.mkdir(parents=True,exist_ok=True)
        new=dict(old,revision=old['revision']+1);new_raw=_encoded(new)
        _durable(journal,_encoded(dict(old_sha256=hashlib.sha256(old_raw).hexdigest(),new_sha256=hashlib.sha256(new_raw).hexdigest(),old_preferences=old_prefs,new_preferences=new_prefs)))
        _durable(self.catalog.path,new_raw);self.catalog._cache=None;self.catalog._stamp=None
        self.assertTrue(recover_replace(self.base,self.project))
        self.assertEqual(json.loads((self.base/'configs/reference_control.json').read_text()),new_prefs)
        self.assertFalse(journal.exists())

    def test_previously_deleted_duplicate_is_rejected(self):
        self.catalog.delete_image(self.old_image,self.catalog.revision)
        self.write_zip(self.zip_path,[('same.png',self.image)])
        scanned=self.importer.scan();entry=self.importer.files[scanned['files'][0]['id']]
        self.importer._build([entry],scanned['catalog_revision'],[])
        self.assertEqual(self.importer.status()['state'],'failed')
        self.assertTrue(self.catalog._load()['images'][self.old_image]['deleted'])


if __name__=='__main__':unittest.main()
