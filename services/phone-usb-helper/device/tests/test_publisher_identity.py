import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('usb_guard',Path(__file__).resolve().parents[1]/'guard.py')
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class PublisherIdentityTests(unittest.TestCase):
 def setUp(self):
  self.meta={'device_id':'cp_0017'}
  self.config=b'[server]\nhost-name=cp-0017\nallow-interfaces=usb1\n'
  self.service=b'<service-group><name>HeadRing CP17</name><service><txt-record>device_id=cp_0017</txt-record></service></service-group>'
 def test_matching_device(self):
  g.validate_publisher_identity(self.meta,self.config,self.service)
 def test_wrong_host_even_with_valid_hash(self):
  with self.assertRaises(g.Refused):g.validate_publisher_identity(self.meta,self.config.replace(b'cp-0017',b'cp-0022'),self.service)
 def test_wrong_service_identity(self):
  for service in (self.service.replace(b'cp_0017',b'cp_0022'),self.service.replace(b'CP17',b'CP22')):
   with self.assertRaises(g.Refused):g.validate_publisher_identity(self.meta,self.config,service)
 def test_duplicate_or_missing_hostname(self):
  for config in (b'[server]\n',self.config+b'host-name=cp-0017\n'):
   with self.assertRaises(g.Refused):g.validate_publisher_identity(self.meta,config,self.service)

if __name__=='__main__':unittest.main()
