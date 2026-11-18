import importlib.util,unittest
from pathlib import Path
s=importlib.util.spec_from_file_location('usbguard',Path(__file__).resolve().parents[1]/'usb_dhcp_guard.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class T(unittest.TestCase):
 def test_exact_legacy_only(self):
  key=('usb0','1','Ifupdown (usb0)','abc');self.assertTrue(m.safe_live_reapply('legacy',key,['10.42.0.2'],True,'manual',False))
  for dec,k,a,c,method,lease in [('legacy',key,['192.168.42.2'],True,'manual',False),('legacy',key,['10.42.0.2'],True,'manual',True),('legacy',key,[],True,'manual',False),('legacy',key,['10.42.0.2'],False,'manual',False),('recover',key,['10.42.0.2'],True,'manual',False),('legacy',('eth0','1','Ifupdown (usb0)','abc'),['10.42.0.2'],True,'manual',False),('legacy',key,['10.42.0.2'],True,'auto',False)]:self.assertFalse(m.safe_live_reapply(dec,k,a,c,method,lease))
 def test_only_device_reapply_no_reconnect_or_usb_reset(self):
  calls=[];m.cmd=lambda a,t=8:calls.append(a);m.reapply_phone_ipv4('usb0');self.assertEqual(calls[0][:4],['/usr/bin/nmcli','device','modify','usb0']);self.assertIn('ipv4.never-default',calls[0]);self.assertIn('ipv4.ignore-auto-dns',calls[0]);self.assertNotIn('up',calls[0])
 def test_busy_does_not_consume_retry(self):
  p=m.RetryPolicy();key=('usb0','1','Ifupdown (usb0)','id');self.assertEqual(p.observe(key,['10.42.0.2'],True,'manual',1),'legacy');self.assertEqual(p.observe(key,['10.42.0.2'],True,'manual',4),'legacy')
unittest.main()
