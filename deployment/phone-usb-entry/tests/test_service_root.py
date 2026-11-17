import importlib.util,tempfile,unittest
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'deploy_bundle.py';spec=importlib.util.spec_from_file_location('rootcheck',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Test(unittest.TestCase):
 def test_direct_and_verified_alias(self):
  with tempfile.TemporaryDirectory() as t:
   base=Path(t);real=base/'real/orangepi';(real/'scripts').mkdir(parents=True);(real/'scripts/run.sh').write_text('x');alias=base/'alias/orangepi';alias.parent.mkdir();alias.symlink_to(real,target_is_directory=True)
   self.assertTrue(m.service_root_matches(real,'argv[]=/bin/bash '+str(real/'scripts/run.sh')+' service',aliases=[alias]))
   self.assertTrue(m.service_root_matches(real,'argv[]=/bin/bash '+str(alias/'scripts/run.sh')+' service',aliases=[alias]))
   self.assertFalse(m.service_root_matches(real,'argv[]=/bin/bash '+str(alias/'scripts/run.sh')+'.evil service',aliases=[alias]))
   other=base/'other/orangepi';(other/'scripts').mkdir(parents=True);(other/'scripts/run.sh').write_text('x')
   self.assertFalse(m.service_root_matches(real,'argv[]=/bin/bash '+str(other/'scripts/run.sh')+' service',aliases=[other]))
if __name__=='__main__':unittest.main()
