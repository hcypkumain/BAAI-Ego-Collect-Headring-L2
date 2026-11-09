import fcntl, importlib.util, json, os, stat, tempfile, unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
usb=load('usb','services/usb-phone-link/usb_phone_link.py')
mount=load('mount','orangepi/scripts/usb_tasks_mount_readonly.py')
phys=[dict(serial='ABC123',usb='1-1',devnum='4')]

class Recovery(unittest.TestCase):
 def test_unknown_and_existing_offline_aggregate(self):
    with patch.object(usb,'pi_id',return_value='pi'):
      m=usb.PhoneMonitor(clock=lambda:0);u=m.unknown('adb_enumeration_timeout')
      self.assertEqual(u['state'],'adb_enumeration_unknown');self.assertFalse(u['connection_known'])
      s=m.refresh([dict(serial='A',usb='1-1',state='offline'),dict(serial='B',usb='1-2',state='offline')],[])
      self.assertEqual(s['state'],'transport_offline')
 def test_no_server_restart_for_offline_and_unknown(self):
    now=[0];r=usb.TransportRecovery(lambda:now[0])
    with patch.object(usb,'adb') as cmd:
      r.recover([],phys)
      for moment in [30,40,100,500]:now[0]=moment;r.recover([],phys)
      self.assertFalse(any(x.args[0] in (['kill-server'],['start-server']) for x in cmd.call_args_list))
 def test_bounded_retry_then_success(self):
    with tempfile.TemporaryDirectory() as t:
      state=Path(t)/'state';sleeps=[]
      def wait(sec):
        sleeps.append(sec)
        if len(sleeps)==7:raise StopIteration
      with patch.object(usb,'STATE',state),patch.object(usb,'physical_adb_devices',return_value=[]),patch.object(usb,'devices',side_effect=[TimeoutError]*6+[[]]),patch.object(usb.time,'sleep',side_effect=wait),patch.object(usb,'pi_id',return_value='pi'):
        with self.assertRaises(StopIteration):usb.monitor()
      self.assertEqual(sleeps,[2,4,8,15,15,15,1])
      self.assertEqual(json.loads(state.read_text())['state'],'phone_missing')
 def test_adb_port_and_environment(self):
    proc=Mock(returncode=0,stdout='ok')
    with patch.dict(os.environ,{'ADB_SERVER_SOCKET':'tcp:example.com:5037','ANDROID_ADB_SERVER_PORT':'5037','ANDROID_SERIAL':'x'}),patch.object(usb.subprocess,'run',return_value=proc) as run:
      usb.adb(['devices','-l'])
      argv=run.call_args.args[0];env=run.call_args.kwargs['env']
      self.assertEqual(argv[1:3],['-P','5038']);self.assertNotIn('-H',argv)
      self.assertNotIn('ADB_SERVER_SOCKET',env);self.assertNotIn('ANDROID_ADB_SERVER_PORT',env);self.assertNotIn('ANDROID_SERIAL',env)

class Mount(unittest.TestCase):
 def setUp(self):self.info=Mock(st_rdev=os.makedev(8,1))
 def check(self,rows):
    with patch.object(mount.subprocess,'check_output',return_value=json.dumps({'filesystems':rows}).encode()),patch.object(mount.os,'stat',return_value=Mock(st_dev=os.makedev(8,1))):
      return mount.mounted_readonly('/dev/sda1','/media/disk',self.info)
 def row(self,**changes):
    row={'id':101,'target':'/media/disk','source':'/dev/sda1','maj:min':'8:1','fstype':'exfat','options':'ro,nodev,nosuid,noexec'};row.update(changes);return row
 def test_exact_ro_only(self):
    good=self.row();self.assertTrue(self.check([good]));self.assertFalse(self.check([self.row(options='rw,nodev')]))
    self.assertFalse(self.check([self.row(target='/media')]))
    self.assertFalse(self.check([self.row(fstype='autofs')]))
    self.assertFalse(self.check([good,self.row(id=102,target='/mnt/other',options='rw')]))
    self.assertFalse(self.check([good,self.row(**{'id':99,'maj:min':'0:50','fstype':'autofs','source':'unexpected'})]))
 def test_normal_systemd_automount_and_rejections(self):
    real=self.row()
    scaffold=self.row(**{'id':99,'source':'systemd-1','maj:min':'0:50','fstype':'autofs','options':'rw,relatime'})
    self.assertTrue(self.check([scaffold,real]))
    self.assertFalse(self.check([scaffold]))
    self.assertFalse(self.check([scaffold,self.row(**{'id':102,'maj:min':'8:2','source':'/dev/sdb1'})]))
    self.assertFalse(self.check([scaffold,self.row(options='rw,nodev')]))
    self.assertFalse(self.check([scaffold,real,self.row(id=102,target='/mnt/other',options='rw')]))
    self.assertFalse(self.check([scaffold,real,self.row(**{'id':102,'maj:min':'8:2','source':'/dev/sdb1'})]))
    self.assertTrue(self.check([scaffold,scaffold,real]))
    self.assertFalse(self.check([scaffold,dict(scaffold,id=98),real]))
    self.assertFalse(self.check([scaffold,real,dict(real,id=103)]))
    self.assertFalse(self.check([scaffold,self.row(options='ro,nodev')]))
    self.assertFalse(self.check([scaffold,self.row(target='/media')]))
 def test_mount_swap_between_verifications_is_rejected(self):
    real=self.row();changed=dict(real,id=102)
    with patch.object(mount.subprocess,'check_output',side_effect=[json.dumps({'filesystems':[real]}).encode(),json.dumps({'filesystems':[changed]}).encode()]),patch.object(mount.os,'stat',return_value=Mock(st_dev=os.makedev(8,1))):
      self.assertFalse(mount.mounted_readonly('/dev/sda1','/media/disk',self.info))
 def test_wrong_visible_layer_is_rejected(self):
    with patch.object(mount.subprocess,'check_output',return_value=json.dumps({'filesystems':[self.row()]}).encode()),patch.object(mount.os,'stat',return_value=Mock(st_dev=os.makedev(8,2))):
      self.assertFalse(mount.mounted_readonly('/dev/sda1','/media/disk',self.info))
 def test_new_automount_triggers_before_verification(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);row={'path':'/dev/sda1','type':'part','fstype':'exfat','mountpoint':None}
      info=Mock(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1));original_stat=os.stat
      def fake_stat(path,*args,**kwargs):
        if str(path)=='/dev/sda1':return info
        if str(path)==str(root/'8-1') and not kwargs.get('follow_symlinks') is False:
          return Mock(st_uid=0,st_mode=stat.S_IFDIR|0o755)
        return original_stat(path,*args,**kwargs)
      events=[];actual_scandir=os.scandir
      def scan(path):events.append('trigger');return actual_scandir(path)
      def verify(*args):events.append('verify');return True
      with patch.object(mount,'ROOT',root),patch.object(mount,'eligible',return_value=True),patch.object(mount.os,'stat',side_effect=fake_stat),patch.object(mount.os,'scandir',side_effect=scan),patch.object(mount,'mounted_readonly',side_effect=verify),patch.object(mount.subprocess,'run',return_value=Mock(returncode=0)) as run:
        out=[];mount.visit([row],out)
      self.assertEqual(events,['trigger','verify']);self.assertEqual(len(out),1)
      self.assertIn('--automount=yes',run.call_args.args[0])
 def test_existing_and_new_systemd_fixture_full_verification(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);dest=root/'8-1';dest.mkdir();real=self.row(target=str(dest))
      scaffold=self.row(id=99,target=str(dest),source='systemd-1',**{'maj:min':'0:50','fstype':'autofs','options':'rw,relatime'})
      payload=json.dumps({'filesystems':[scaffold,real]}).encode()
      info=Mock(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1));original=os.stat
      def fake_stat(path,*args,**kwargs):
        if str(path)=='/dev/sda1':return info
        if str(path)==str(dest) and kwargs.get('follow_symlinks') is not False:
          return Mock(st_uid=0,st_mode=stat.S_IFDIR|0o755,st_dev=os.makedev(8,1))
        return original(path,*args,**kwargs)
      row={'path':'/dev/sda1','type':'part','fstype':'exfat','mountpoint':str(dest)}
      with patch.object(mount,'ROOT',root),patch.object(mount,'eligible',return_value=True),patch.object(mount.os,'stat',side_effect=fake_stat),patch.object(mount.subprocess,'check_output',return_value=payload),patch.object(mount.subprocess,'run',return_value=Mock(returncode=0)) as run:
        out=[];mount.visit([row],out);self.assertEqual(len(out),1);run.assert_not_called()
        row['mountpoint']=None;out=[];mount.visit([row],out)
        self.assertEqual(len(out),1);run.assert_called_once();self.assertIn('--automount=yes',run.call_args.args[0])
 def test_existing_and_new_mounts_are_rechecked(self):
    with tempfile.TemporaryDirectory() as tmp:
      root=Path(tmp);existing=root/'existing';existing.mkdir()
      row={'path':'/dev/sda1','type':'part','fstype':'exfat','mountpoint':str(existing)}
      info=Mock(st_mode=stat.S_IFBLK,st_rdev=os.makedev(8,1))
      original_stat=os.stat
      def fake_stat(path,*args,**kwargs):
        if str(path)=='/dev/sda1':return info
        if str(path)==str(root/'8-1') and not kwargs.get('follow_symlinks') is False:
          return Mock(st_uid=0,st_mode=stat.S_IFDIR|0o755)
        return original_stat(path,*args,**kwargs)
      with patch.object(mount,'ROOT',root),patch.object(mount,'eligible',return_value=True),patch.object(mount.os,'stat',side_effect=fake_stat),patch.object(mount,'mounted_readonly',return_value=False) as verify,patch.object(mount.subprocess,'run',return_value=Mock(returncode=0)) as run:
        out=[];mount.visit([row],out);self.assertEqual(out,[]);run.assert_not_called();verify.assert_called_once()
        verify.reset_mock();row['mountpoint']=None;mount.visit([row],out)
        self.assertEqual(out,[]);run.assert_called_once();verify.assert_called_once()
 def test_findmnt_failure_closed(self):
    with patch.object(mount.subprocess,'check_output',side_effect=TimeoutError):self.assertFalse(mount.mounted_readonly('/dev/sda1','/media/disk',self.info))

if __name__=='__main__':unittest.main()
