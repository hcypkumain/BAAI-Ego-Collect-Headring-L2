import sys, unittest, subprocess, json, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src/orangepi_collect'/name) for name in ('control','common')]
from usb_export_profile import snapshot, discover_sources, check_legacy_data

class ProfileSnapshotTests(unittest.TestCase):
 def test_legacy_data_never_silently_omitted(self):
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'data/baai_ego_task';path.mkdir(parents=True);check_legacy_data(temp)
   (path/'old-batch').mkdir()
   with self.assertRaises(ValueError):check_legacy_data(temp)
 def test_empty_cleanup_directory_is_skipped_but_nonempty_blocks(self):
  with tempfile.TemporaryDirectory() as temp:
   shift=Path(temp)/'day';shift.mkdir();trash=shift/'.usb-export-cleanup';trash.mkdir()
   take=shift/'take';take.mkdir();self.assertEqual(discover_sources(temp),[str(take)])
   (trash/'pending').mkdir()
   with self.assertRaises(ValueError):discover_sources(temp)
 def test_profiles_use_distinct_process_contexts(self):
  code="import json,usb_export_profile as p,ledger; x=p.configure(__MODE__); print(json.dumps([str(x.BASE),str(x.PROJECT),x.COLLECTION_MODE]))"
  # Import the ledger after selecting the process-local profile.
  code=code.replace('p,ledger; x=p.configure(__MODE__);','p; x=p.configure(__MODE__); import ledger;')
  env={'PYTHONPATH':str(ROOT/'src/orangepi_collect/control')}
  import os
  env=dict(os.environ,**env)
  values=[]
  for mode in ('emg','eeg'):
   raw=subprocess.check_output([sys.executable,'-c',code.replace('__MODE__',repr(mode))],env=env)
   values.append(json.loads(raw))
  self.assertNotEqual(values[0][0],values[1][0]);self.assertEqual([v[2] for v in values],['emg','eeg'])
 def test_states_are_preserved_without_cross_promoting_pending(self):
  for status,qualified in [('success',True),('pending_qc',False),('error',False)]:
   meta=dict(status=status,qualified=qualified,_image=Path('/tmp/reference'))
   store=SimpleNamespace(metadata=lambda source:meta)
   with patch('daily_collection.take_snapshot',return_value=dict(status=status,qualified=qualified)):
    result=snapshot(store,Path('/tmp/take'))
   self.assertEqual(result['status'],status);self.assertEqual(result['qualified'],qualified)
   self.assertNotIn('_image',result);self.assertEqual(result['daily_snapshot']['status'],status)
