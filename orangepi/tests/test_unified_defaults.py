"""EEG deployment defaults preserve history and ordinary startup selections."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import collection_profiles

class DefaultsTests(unittest.TestCase):
    def test_missing_default_uses_eeg(self):
        with tempfile.TemporaryDirectory() as root:
            value=collection_profiles.selection(root)
            self.assertEqual(value['active_mode'],'eeg')
            self.assertEqual(value['legacy_mode'],'eeg')
            self.assertEqual(value['generation'],0)

    def test_repository_template_defaults_to_eeg(self):
        path=Path(__file__).resolve().parents[1]/'configs/collection_mode.json'
        self.assertEqual(json.loads(path.read_text())['default_mode'],'eeg')

    def test_existing_emg_mode_wins_over_repository_eeg_default(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'runtime').mkdir();(root/'configs').mkdir()
            old=dict(version=1,active_mode='emg',legacy_mode='emg',generation=7)
            (root/'runtime/collection_profile.json').write_text(json.dumps(old))
            (root/'configs/collection_mode.json').write_text('{"default_mode":"eeg"}')
            self.assertEqual(collection_profiles.selection(root),old)

    def test_explicit_emg_template_is_still_supported(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'configs').mkdir()
            (root/'configs/collection_mode.json').write_text('{"default_mode":"emg"}')
            self.assertEqual(collection_profiles.selection(root)['active_mode'],'emg')


class DeploymentDefaultsTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[2]/'tools/configure_collection_profiles.py'
        spec=importlib.util.spec_from_file_location('configure_collection_profiles_test',path)
        self.cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.cli)

    def configure(self,root,legacy='emg',active=None):
        args=['configure_collection_profiles.py','--project-root',str(root),'--legacy-mode',legacy]
        if active is not None:args+=['--active-mode',active]
        output=io.StringIO();search_path=list(sys.path)
        # Provisioning hardware/config copies are independent of mode selection.
        # Use the real initialize/activate and atomic profile writes on temp files.
        try:
            with patch.object(sys,'argv',args),patch.object(collection_profiles,'provision'),contextlib.redirect_stdout(output):
                self.cli.main()
        finally:sys.path[:]=search_path
        return json.loads(output.getvalue())

    def station(self,root,old=None):
        base=root/'orangepi';(base/'runtime').mkdir(parents=True)
        sentinel=base/'legacy-emg-sentinel.txt';sentinel.write_text('existing EMG history')
        if old is not None:(base/'runtime/collection_profile.json').write_text(json.dumps(old))
        return base,sentinel

    def test_emg_history_starts_in_eeg_without_relabelling(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base,sentinel=self.station(root)
            value=self.configure(root)
            self.assertEqual(value,dict(version=1,active_mode='eeg',legacy_mode='emg',generation=2))
            self.assertEqual(sentinel.read_text(),'existing EMG history')
            self.assertEqual(collection_profiles.profile_base(base,value,'emg'),base.resolve())
            self.assertNotEqual(collection_profiles.profile_base(base,value,'eeg'),base.resolve())

    def test_eeg_history_needs_no_extra_mode_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.station(root)
            self.assertEqual(self.configure(root,legacy='eeg'),dict(version=1,active_mode='eeg',legacy_mode='eeg',generation=1))

    def test_explicit_emg_deployment_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.station(root)
            self.assertEqual(self.configure(root,active='emg'),dict(version=1,active_mode='emg',legacy_mode='emg',generation=1))

    def test_deployment_selects_eeg_without_changing_existing_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=dict(version=1,active_mode='emg',legacy_mode='emg',generation=7)
            self.station(root,old)
            self.assertEqual(self.configure(root),dict(old,active_mode='eeg',generation=8))

    def test_repeated_eeg_deployment_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=dict(version=1,active_mode='eeg',legacy_mode='emg',generation=7)
            self.station(root,old)
            self.assertEqual(self.configure(root),old)

    def test_historical_ownership_cannot_be_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=dict(version=1,active_mode='emg',legacy_mode='emg',generation=7)
            base,_=self.station(root,old)
            with self.assertRaisesRegex(ValueError,'已有文件的采集模式归属不能改变'):
                self.configure(root,legacy='eeg')
            self.assertEqual(collection_profiles.selection(base),old)

    def test_active_recording_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base,_=self.station(root)
            (base/'runtime/controller.json').write_text('{"state":"recording"}')
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as result:
                self.configure(root)
            self.assertEqual(result.exception.code,2)
            self.assertFalse((base/'runtime/collection_profile.json').exists())

if __name__=='__main__':unittest.main()
