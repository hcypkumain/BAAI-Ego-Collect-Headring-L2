import importlib.machinery,importlib.util,pathlib,tempfile,unittest,os,stat,subprocess
from types import SimpleNamespace
from unittest.mock import patch
SRC=pathlib.Path(__file__).resolve().parents[1]/'system/mount-readonly'
loader=importlib.machinery.SourceFileLoader('mount_helper',str(SRC));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
class Tests(unittest.TestCase):
 def test_eligible_only_external_usb_filesystems(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t);(p/'usb7/sda1').mkdir(parents=True);(p/'sys').mkdir();(p/'sys/sda1').symlink_to(p/'usb7/sda1')
   row=dict(path='/dev/sda1',type='part',fstype='exfat',mountpoint=None)
   self.assertTrue(m.eligible(row,p/'sys'))
   self.assertFalse(m.eligible(dict(row,path='/dev/mmcblk0p1'),p/'sys'))
   self.assertFalse(m.eligible(dict(row,mountpoint='/'),p/'sys'))
   self.assertFalse(m.eligible(dict(row,mountpoint='/media/orangepi/sdcard'),p/'sys'))
   self.assertFalse(m.eligible(dict(row,fstype='crypto_LUKS'),p/'sys'))
   self.assertTrue(m.eligible(dict(row,fstype='ntfs3'),p/'sys'))
 def test_mount_is_readonly_device_bound_and_no_fsck(self):
  cmd=m.mount_command('/dev/sda1','/run/example','ext4')
  self.assertIn('--bind-device',cmd);self.assertIn('--fsck=no',cmd);self.assertIn('ro,nodev,nosuid,noexec,noload',cmd)
  self.assertNotIn('rw',cmd)
 def exercise(self,first_missing=False,first_mount_fails=False,first_timeout=False):
  with tempfile.TemporaryDirectory() as t:
   root=pathlib.Path(t);realstat=os.stat
   def fake_stat(path,*a,**kw):
    value=str(path)
    if value.startswith('/dev/sd'):
     if first_missing and value=='/dev/sda1':raise FileNotFoundError(value)
     return SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1 if value=='/dev/sda1' else 17))
    r=realstat(path,*a,**kw)
    if pathlib.Path(path).parent==root:
     v=list(r);v[4]=0;return os.stat_result(v)
    return r
   calls=[]
   def run(cmd,**kw):
    calls.append(cmd)
    if cmd[-2]=='/dev/sda1':
     if first_timeout:raise subprocess.TimeoutExpired(cmd,15)
     if first_mount_fails:return SimpleNamespace(returncode=1,stderr=b'unsupported filesystem')
    return SimpleNamespace(returncode=0,stderr=b'')
   rows=[dict(path=x,type='part',fstype='exfat',mountpoint=None) for x in ['/dev/sda1','/dev/sdb1']]
   volumes=[];errors=[]
   with patch.object(m,'ROOT',root),patch.object(m,'eligible',return_value=True),patch.object(m.os,'stat',side_effect=fake_stat),patch.object(m.subprocess,'run',side_effect=run):
    m.visit(rows,volumes,errors)
   return volumes,errors,calls
 def test_unplug_one_device_does_not_hide_other_disk(self):
  volumes,errors,_=self.exercise(first_missing=True)
  self.assertEqual([x['device'] for x in volumes],['/dev/sdb1']);self.assertEqual(errors[0]['stage'],'discover')
 def test_failed_mount_reports_reason_and_continues(self):
  volumes,errors,_=self.exercise(first_mount_fails=True)
  self.assertEqual([x['device'] for x in volumes],['/dev/sdb1']);self.assertIn('unsupported',errors[0]['error'])
 def test_slow_device_does_not_abort_remaining_scan(self):
  volumes,errors,_=self.exercise(first_timeout=True)
  self.assertEqual([x['device'] for x in volumes],['/dev/sdb1']);self.assertEqual(len(errors),1)
 def test_successful_scan_returns_all_volumes(self):
  volumes,errors,calls=self.exercise();self.assertEqual(len(volumes),2);self.assertEqual(errors,[])
 def test_timer_retries_after_missed_hotplug_and_delayed_identification(self):
  text=(SRC.parent/'collector-usb-task-mount.timer').read_text();self.assertIn('OnUnitInactiveSec=5s',text)
  self.assertIn('collector-usb-task-mount.service',text)
if __name__=='__main__':unittest.main(verbosity=2)
