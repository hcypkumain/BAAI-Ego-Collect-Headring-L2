import importlib.util,json,unittest
from unittest.mock import patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('phone_links',Path(__file__).resolve().parents[1]/'src/orangepi_collect/control/phone_links.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
class Tests(unittest.TestCase):
 def test_interfaces(self):
  rows=[{'ifname':'lo','flags':['UP'],'addr_info':[{'local':'127.0.0.1'}]}, {'ifname':'wlan0','flags':['UP'],'addr_info':[{'local':'192.168.20.107'},{'local':'fe80::1'},{'local':'bad'}]}, {'ifname':'usb0','flags':['UP'],'addr_info':[{'local':'192.168.9.10'}]}, {'ifname':'eth0','flags':[],'addr_info':[{'local':'10.0.0.1'}]}]
  with patch.object(mod.subprocess,'check_output',return_value=json.dumps(rows).encode()): v=mod.interface_addresses()
  self.assertEqual({(i['interface'],i['kind'],i['address']) for i in v},{('wlan0','wireless','192.168.20.107'),('wlan0','wireless','fe80::1'),('usb0','usb','192.168.9.10')})
 def test_error(self):
  with patch.object(mod.subprocess,'check_output',side_effect=OSError()): self.assertIsNone(mod.interface_addresses())
if __name__=='__main__': unittest.main()
