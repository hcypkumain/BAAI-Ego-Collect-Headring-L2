import sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths,storage_paths
class StorageTests(unittest.TestCase):
 def test_missing_mount_refuses_internal_fallback(self):
  with patch('storage_paths.subprocess.check_output',side_effect=OSError('missing')):
   with self.assertRaisesRegex(storage_paths.StorageError,'未挂载'):storage_paths.volume_root()
 def test_wrong_uuid_refused(self):
  data={'filesystems':[{'uuid':'different'}]}
  with patch('storage_paths.subprocess.check_output',return_value=json.dumps(data)):
   with self.assertRaisesRegex(storage_paths.StorageError,'身份'):storage_paths.volume_root()
 def test_system_disk_refused_even_if_uuid_matches(self):
  cfg=storage_paths.config();cfg['mount_point']='/'
  data={'filesystems':[{'uuid':cfg['expected_uuid'],'target':'/','options':'rw'}]}
  with patch('storage_paths.config',return_value=cfg),patch('storage_paths.subprocess.check_output',return_value=json.dumps(data)):
   with self.assertRaisesRegex(storage_paths.StorageError,'系统盘'):storage_paths.volume_root()
 def test_project_can_itself_be_on_the_sd_card(self):
  from types import SimpleNamespace
  cfg=storage_paths.config();cfg['mount_point']='/fake_sd'
  info={'filesystems':[{'uuid':cfg['expected_uuid'],'target':'/fake_sd','options':'rw'}]}
  with patch('storage_paths.config',return_value=cfg),patch('storage_paths.PROJECT',Path('/fake_sd/app')),patch('storage_paths.subprocess.check_output',return_value=json.dumps(info)),patch.object(Path,'stat',autospec=True,side_effect=lambda p,**kwargs:SimpleNamespace(st_dev=1 if str(p)=='/' else 2)):
   self.assertEqual(storage_paths.volume_root(),Path('/fake_sd')/cfg['directory']/cfg['device_id'])
 def test_output_outside_card_refused(self):
  with patch('storage_paths.volume_root',return_value=Path('/sd/collector')):
   with self.assertRaises(storage_paths.StorageError):storage_paths.validate_output('/tmp/session')
if __name__=='__main__':unittest.main()
