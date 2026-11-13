import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path=Path(__file__).resolve().parents[1]/'guard.py'
spec=importlib.util.spec_from_file_location('phone_guard',path)
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
SEL={'usb_port':'7-1.2.2.2','drivers':['cdc_ether','cdc_ncm','rndis_host']}
UNIQUE={'mode':'unique-usb-network','drivers':['cdc_ether','cdc_ncm','rndis_host']}
class SelectorTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);self.net=self.base/'net';self.net.mkdir();self.usb_bus=self.base/'bus/usb';self.drivers=self.usb_bus/'drivers';self.drivers.mkdir(parents=True)
 def device(self,name,port,driver,vid='9999',pid='1234'):
  usb=self.base/'devices'/port;usb.mkdir(parents=True,exist_ok=True);(usb/'idVendor').write_text(vid);(usb/'idProduct').write_text(pid)
  interface=usb/(port+':1.0');interface.mkdir(exist_ok=True);drv=self.drivers/driver;drv.mkdir(exist_ok=True);(interface/'driver').symlink_to(drv);(interface/'subsystem').symlink_to(self.usb_bus)
  net=self.net/name;net.mkdir();(net/'device').symlink_to(interface)
 def test_three_usb_network_drivers_and_any_vendor(self):
  for driver in SEL['drivers']:
   with self.subTest(driver=driver),tempfile.TemporaryDirectory() as root:
    self.base=Path(root);self.net=self.base/'net';self.net.mkdir();self.usb_bus=self.base/'bus/usb';self.drivers=self.usb_bus/'drivers';self.drivers.mkdir(parents=True);self.device('usb0','7-1.2.2.2',driver)
    self.assertTrue(guard.Sysfs(self.net,self.usb_bus).matches('usb0',SEL))
 def test_other_port_and_non_network_driver_rejected(self):
  self.device('usb0','7-1.2.3','rndis_host');self.device('usb1','7-1.2.2.2','usb_storage');self.assertFalse(guard.Sysfs(self.net,self.usb_bus).matches('usb0',SEL));self.assertFalse(guard.Sysfs(self.net,self.usb_bus).matches('usb1',SEL))
 def test_unique_mode_allows_physical_ports_not_storage(self):
  self.device('usb0','7-1.3.3','rndis_host',vid='1abc',pid='9876');self.device('usb1','7-1.2.2.2','usb_storage')
  self.assertTrue(guard.Sysfs(self.net,self.usb_bus).matches('usb0',UNIQUE));self.assertFalse(guard.Sysfs(self.net,self.usb_bus).matches('usb1',UNIQUE))
 def test_unique_mode_metadata_rejects_unknown_driver(self):
  value={'schema':1,'device_id':'cp_0015','interface':'usb1','selector':UNIQUE,'binary_path':'/opt/x/avahi-daemon','binary_sha256':'0'*64,'config_path':'/opt/x/avahi-daemon.conf','config_sha256':'0'*64}
  self.assertEqual(guard.metadata(json.dumps(value).encode())['selector'],UNIQUE)
  value['selector']={'mode':'unique-usb-network','drivers':['g_ether']}
  with self.assertRaises(guard.Refused):guard.metadata(json.dumps(value).encode())
 def test_selector_is_port_and_network_drivers_only(self):
  value={'schema':1,'device_id':'cp_0018','interface':'usb1','selector':SEL,'binary_path':'/opt/x/avahi-daemon','binary_sha256':'0'*64,'config_path':'/opt/x/avahi-daemon.conf','config_sha256':'0'*64}
  self.assertEqual(guard.metadata(json.dumps(value).encode())['selector'],SEL)
  for bad in ({'usb_port':SEL['usb_port'],'drivers':['usb_storage']},{'usb_port':SEL['usb_port'],'vid':'2717','pid':'ff80'}):
   value['selector']=bad
   with self.assertRaises(guard.Refused):guard.metadata(json.dumps(value).encode())
if __name__=='__main__':unittest.main()
