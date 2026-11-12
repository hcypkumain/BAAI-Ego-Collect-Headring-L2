import base64,hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import metrics,jobs,web_config,operations
from host.qc.tests.governance_fixtures import sample_registry
import importlib.util
spec=importlib.util.spec_from_file_location('phase_probe',ROOT/'resources/metrics_probe.py');probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
class PhaseTests(unittest.TestCase):
 def setUp(self):
  self.registry=sample_registry(('cp_0014',))
  self.config=operations.bound_config(web_config.setup(registry=self.registry,selected=['cp_0014'],config={'ssh_config':'x'}),self.registry)
  p=patch.object(metrics.online,'config_for',return_value={'user':'fixture','hostkeyalias':'test-key-cp_0014'})
  p.start();self.addCleanup(p.stop)
 def test_profile_selection_eeg_legacy(self):
  with tempfile.TemporaryDirectory() as t:
   code=Path(t)/'orangepi';code.mkdir()
   r=probe.profile_details(code,{'legacy_mode':'eeg','active_mode':'emg'},[])
   self.assertTrue(r['eeg']['legacy']);self.assertFalse(r['emg']['legacy']);self.assertEqual(r['emg']['catalog']['status'],'absent')
 def test_invalid_selection(self):
  self.assertEqual(probe.profile_details(Path('/nonexistent'),{'legacy_mode':'x'},[])['emg']['root_status'],'invalid_selection')
 def test_bad_json_and_absence(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'x';p.write_text('{')
   self.assertEqual(probe.read_reason(p)[1],'invalidjson');self.assertEqual(probe.read_reason(p.parent/'missing')[1],'absent')
 def test_cache_backup_and_interrupted_jobs(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);(p/'cache.json').write_text('{"cp_0014":{"online":true}}');(p/'jobs.json').write_text('{"abc":{"status":"running"}}')
   registry=sample_registry(('cp_0014',))
   owner=jobs.Jobs(p,web_config.setup(registry=registry,selected=['cp_0014'],config={'ssh_config':'synthetic'}),registry=registry)
   try:
    self.assertEqual(owner.jobs['abc']['status'],'interrupted');self.assertTrue((p/'cache-v1-backup.json').exists());self.assertFalse(owner.cache['cp_0014']['online'])
   finally:owner.lockfile.close()
 def test_plan_no_remote_mutation(self):
  with tempfile.TemporaryDirectory() as t:
   config=self.config
   class S:
    def __init__(self,*args): self.argv=['ssh',args[1]]
    def run(self,cmd): self.assert_safe(cmd);return b'cp_0014\n'
    def assert_safe(self,cmd):
     if cmd!='cat /opt/egorecord/DEVICE_ID': raise AssertionError(cmd)
   live={'a':1}; plan={'device':'cp_0014','diff':[],'protected':{},'component_diff':[]}
   with patch.object(metrics.online,'SSH',S),patch.object(metrics.online,'read_live',return_value=live),patch.object(metrics.online,'prepare_plan',return_value=plan),patch.object(metrics.online,'write_plan',return_value=('a'*64,None)),patch('fleet.build',return_value={'manifest_sha256':'b'*64}):
    r=metrics.create_plan(config,'cp_0014',t);self.assertEqual(r['plan_sha256'],'a'*64);self.assertEqual(r['apply'],'not_available_in_web')
 def test_original_integrity_and_size(self):
  with tempfile.TemporaryDirectory() as t:
   buf=io.BytesIO();Image.new('RGB',(4,5)).save(buf,format='PNG');raw=buf.getvalue(); ident='image_'+'a'*40
   value={'id':ident,'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
   class P: returncode=0;stdout=json.dumps(value).encode()
   class S:
    def __init__(self,*a):self.argv=['ssh',a[1]]
   cfg=self.config
   with patch.object(metrics.online,'SSH',S),patch.object(metrics.subprocess,'run',return_value=P()):
    result=metrics.fetch_original(cfg,'cp_0014','emg',1,ident,t);self.assertEqual((result['width'],result['height']),(4,5)); self.assertEqual(os.stat(Path(t)/'assets'/result['key']).st_mode&0o777,0o600)
    value['sha256']='0'*64;P.stdout=json.dumps(value).encode()
    with self.assertRaises(ValueError):metrics.fetch_original(cfg,'cp_0014','emg',1,ident,t)
 def test_image_id_validation(self):
  with self.assertRaises(ValueError): metrics.fetch_original({'devices':{'cp_0014':'192.168.20.111'}},'cp_0014','emg',1,'../etc/passwd','/tmp')
if __name__=='__main__': unittest.main()
