import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import headring_capture_guard as g
class Discover(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.projects=[self.root/'main',self.root/'main.2']
  for project in self.projects:
   (project/'orangepi/scripts').mkdir(parents=True);(project/'orangepi/scripts/run.sh').write_text('#!/bin/sh\n')
  self.allowed=patch.object(g,'ALLOWED_PROJECTS',frozenset(self.projects));self.allowed.start();self.addCleanup(self.allowed.stop)
 def fake(self,working,execution):
  def run(args,**kwargs):return SimpleNamespace(stdout=(working if '--property=WorkingDirectory' in args else execution))
  return run
 def test_supported_service_roots(self):
  for root in self.projects:
   execution='{ path=/bin/bash ; argv[]=/bin/bash '+str(root/'orangepi/scripts/run.sh')+' service ; }'
   with self.subTest(root=root),patch.object(g.subprocess,'run',side_effect=self.fake(str(root),execution)):
    self.assertEqual(g.discover_base(),root/'orangepi')
 def test_alias_to_other_supported_collector_is_canonicalized(self):
  import shutil
  shutil.rmtree(self.projects[0]/'orangepi');(self.projects[0]/'orangepi').symlink_to(self.projects[1]/'orangepi',target_is_directory=True)
  script=self.projects[0]/'orangepi/scripts/run.sh'
  with patch.object(g.subprocess,'run',side_effect=self.fake(str(self.projects[0]),str(script))):self.assertEqual(g.discover_base(),self.projects[1]/'orangepi')
 def test_unknown_or_mismatched_service_is_rejected(self):
  with patch.object(g.subprocess,'run',side_effect=self.fake('/tmp/unknown','/tmp/unknown/orangepi/scripts/run.sh')),self.assertRaises(ValueError):g.discover_base()
  with patch.object(g.subprocess,'run',side_effect=self.fake(str(self.projects[0]),'/some/other/script')),self.assertRaises(ValueError):g.discover_base()
 def test_alias_outside_supported_roots_is_rejected(self):
  import shutil
  outside=self.root/'outside';(outside/'scripts').mkdir(parents=True);(outside/'scripts/run.sh').write_text('#!/bin/sh\n');shutil.rmtree(self.projects[0]/'orangepi');(self.projects[0]/'orangepi').symlink_to(outside,target_is_directory=True)
  with patch.object(g.subprocess,'run',side_effect=self.fake(str(self.projects[0]),str(self.projects[0]/'orangepi/scripts/run.sh'))),self.assertRaises(ValueError):g.discover_base()
if __name__=='__main__':unittest.main()
