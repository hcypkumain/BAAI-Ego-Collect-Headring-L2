"""Scene bytes are immutable across catalog revision conflicts and updates."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CONTROL = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control'
COMMON = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'
sys.path[:0] = [str(CONTROL), str(COMMON)]
import reference_catalog as catalog_module
from reference_catalog import CatalogError, ReferenceCatalog


class SceneTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.base = self.project / 'orangepi'
        self.base.mkdir()
        self.catalog = ReferenceCatalog(self.base, project=self.project)
        self.identifier = self.catalog.create_list('scene', self.catalog.revision)['id']
        self.catalog.set_list_scene(self.identifier, 'scene', None,
                                    {'config.yaml': b'original: true\n'},
                                    self.catalog.revision)

    def scene_entry(self, identifier=None):
        identifier = identifier or self.identifier
        return copy.deepcopy(self.catalog._load()['lists'][identifier]['scene']['files']['config.yaml'])

    def path(self, entry):
        return self.catalog.library / entry['path']

    def assert_file_matches(self, entry, expected):
        raw = self.path(entry).read_bytes()
        self.assertEqual(raw, expected)
        self.assertEqual(len(raw), entry['size_bytes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['sha256'])

    def test_revision_conflict_keeps_previous_metadata_and_bytes(self):
        before = self.scene_entry()
        old_path = self.path(before)
        expected_revision = self.catalog.revision
        fired = False
        sync_directory = catalog_module._sync_directory

        def rename_after_scene_bytes_are_durable(path):
            nonlocal fired
            sync_directory(path)
            if Path(path).name == self.identifier and not fired:
                fired = True
                self.catalog.rename_list(self.identifier, 'concurrent rename', self.catalog.revision)

        with patch.object(catalog_module, '_sync_directory', side_effect=rename_after_scene_bytes_are_durable):
            with self.assertRaises(CatalogError) as raised:
                self.catalog.set_list_scene(self.identifier, 'replacement', None,
                                            {'config.yaml': b'replacement: true\n'},
                                            expected_revision)

        self.assertTrue(fired)
        self.assertEqual(raised.exception.code, 'revision_conflict')
        current = self.scene_entry()
        self.assertEqual(current, before)
        self.assertEqual(self.path(current), old_path)
        self.assert_file_matches(current, b'original: true\n')

    def test_same_content_retry_reuses_immutable_file_without_revision_change(self):
        before = self.scene_entry()
        revision = self.catalog.revision
        result = self.catalog.set_list_scene(self.identifier, 'scene', None,
                                             {'config.yaml': b'original: true\n'}, revision)
        self.assertEqual(result['revision'], revision)
        self.assertEqual(self.scene_entry(), before)
        self.assert_file_matches(before, b'original: true\n')

    def test_successful_replacement_keeps_cloned_scene_bytes_unchanged(self):
        old = self.scene_entry()
        old_path = self.path(old)
        clone = self.catalog.clone_list(self.identifier, 'scene copy', self.catalog.revision)
        cloned = self.scene_entry(clone['id'])
        self.assertEqual(cloned, old)

        result = self.catalog.set_list_scene(self.identifier, 'updated scene', None,
                                             {'config.yaml': b'replacement: true\n'},
                                             self.catalog.revision)
        current = self.scene_entry()
        self.assertEqual(result['files'], ['config.yaml'])
        self.assertNotEqual(current['path'], old['path'])
        self.assert_file_matches(current, b'replacement: true\n')
        self.assertEqual(self.scene_entry(clone['id']), cloned)
        self.assertEqual(self.path(old), old_path)
        self.assert_file_matches(cloned, b'original: true\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
