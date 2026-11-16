import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
ROOT=HERE.parents[1]
BASELINE=ROOT.parent/'source' if not (ROOT/'.git').exists() else None

def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

b=load('saved_default_builder',HERE/'build_bundle.py')
d=load('saved_default_installer',HERE/'deploy_bundle.py')

class DefaultReleaseTests(unittest.TestCase):
 def git(self,*args):
  if BASELINE is None or not BASELINE.is_dir():return subprocess.check_output(['git','-C',str(ROOT)]+list(args))
  if args[0]=='ls-tree':
   paths=sorted(p.relative_to(BASELINE).as_posix() for p in (BASELINE/'orangepi/src/orangepi_collect').rglob('*') if p.is_file() and p.suffix in ('.py','.js','.html','.css'))
   return ('\n'.join(paths)+'\n').encode()
  if args[0]=='show':return (BASELINE/args[1].split(':',1)[1]).read_bytes()
  raise AssertionError(args)
 def test_default_payload_contains_saved_rotation_not_old_git_source(self):
  with patch.object(b,'git',self.git):sources,profile,index=b.collector_sources()
  self.assertEqual(len(sources),139)
  self.assertEqual(profile['rotation']['count_basis'],'saved')
  self.assertIn(b"settings.get('count_basis','saved')",sources['orangepi/src/orangepi_collect/control/task_block_sampling.py'])
  self.assertIn(b'saved_history',sources['orangepi/src/orangepi_collect/control/task_block_sampling.py'])
  self.assertIn(b'ensure_default_policy',sources['orangepi/src/orangepi_collect/control/reference_controls.py'])
  for path,data in sources.items():
   expected=(HERE/'collector-overrides'/path).read_bytes() if path in profile['overrides'] else self.git('show',b.BASELINE+':'+path)
   self.assertEqual(data,expected)
  self.assertEqual(len(index),64)
 def test_altered_frozen_input_is_rejected(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);directory=root/'deployment/phone-usb-entry';shutil.copytree(HERE/'collector-overrides',directory/'collector-overrides')
   shutil.copy2(HERE/'collector-release.json',directory/'collector-release.json')
   path=next((directory/'collector-overrides').rglob('task_block_sampling.py'));path.write_bytes(path.read_bytes()+b'\n# unreviewed\n')
   with patch.object(b,'ROOT',root),patch.object(b,'git',self.git),self.assertRaises(ValueError):b.collector_sources()
 def test_missing_profile_does_not_fall_back_to_old_default(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(b,'ROOT',Path(temp)),self.assertRaises(FileNotFoundError):b.collector_sources()
 def test_task_progress_snapshot_includes_absent_files_and_text_scope(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(d.os,'fchown',lambda *_:None):
   root=Path(temp)/'project/orangepi';(root/'runtime/text_task_blocks').mkdir(parents=True)
   progress=root/'runtime/reference_task_blocks.json';progress.write_text('{"version":3}')
   scoped=root/'runtime/text_task_blocks/blocks-temporary-case.json';scoped.write_text('{"saved":4}')
   tx=Path(temp)/'tx';tx.mkdir();j={}
   d.backup_task_state(root,tx,j)
   self.assertTrue(any(row['target']==str(root/'configs/task_block_policy.json') and not row['existed'] for row in j['task_state_backup']))
   progress.write_text('{"version":4}');scoped.write_text('{"saved":5}')
   d.restore_task_state(tx,j)
   self.assertEqual(progress.read_text(),'{"version":3}')
   self.assertEqual(scoped.read_text(),'{"saved":4}')
 def test_completed_release_rollback_refuses_to_discard_new_progress(self):
  with tempfile.TemporaryDirectory() as temp,patch.object(d.os,'fchown',lambda *_:None):
   root=Path(temp)/'project/orangepi';(root/'runtime').mkdir(parents=True)
   progress=root/'runtime/reference_task_blocks.json';progress.write_text('{"saved":1}')
   tx=Path(temp)/'tx';tx.mkdir();j={'status':'complete','files':[]}
   d.backup_task_state(root,tx,j);progress.write_text('{"saved":2}')
   with patch.object(d,'cmd') as command,self.assertRaises(RuntimeError):d.rollback(tx,j)
   command.assert_not_called();self.assertEqual(progress.read_text(),'{"saved":2}')

if __name__=='__main__':unittest.main()
