import sys,tempfile,unittest,struct
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'services/phone-media'))
import classic_bluetooth_mode as mode
from test_headphone_manager import HeadphoneTests

class ConfigTests(unittest.TestCase):
 def test_config_modes_and_failures(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'main.conf';self.assertEqual(mode.disk_mode(p),'default_dual')
   for contents,expected in [('[General]\nControllerMode=le\n','le'),('[General]\nControllerMode=dual\n','dual'),('[General]\n','dual'),('[General]\nControllerMode=bad\n','unknown'),('[General]\nControllerMode=le\nControllerMode=dual\n','unknown')]:
    p.write_text(contents);self.assertEqual(mode.disk_mode(p),expected)
   with patch.object(Path,'open',side_effect=PermissionError):self.assertEqual(mode.disk_mode(p),'unknown')
 def test_evidence_is_kept_separate(self):
  yes={'supported':True,'enabled':True};no={'supported':True,'enabled':False};unsupported={'supported':False,'enabled':False}
  for disk,runtime,expected in [('le',None,('','classic_config_le_unknown')),('dual',None,('','classic_runtime_unknown')),('unknown',None,('','classic_runtime_unknown')),('le',yes,('','classic_config_le_enabled')),('le',no,('classic_disabled','')),('dual',no,('classic_not_loaded','')),('default_dual',no,('classic_not_loaded','')),('dual',unsupported,('classic_unsupported',''))]:
   self.assertEqual(mode.assess(disk,runtime),expected)
 def test_runtime_reads_only_targeted_read_info(self):
  sock=Mock();sock.fileno.return_value=99
  info=b'\0'*9+struct.pack('<II',0x80,0x80)+b'\0'*8
  sock.recv.return_value=struct.pack('<HHHHB',1,1,3+len(info),4,0)+info
  libc=Mock();libc.bind.return_value=0
  with patch.object(mode.socket,'socket',return_value=sock),patch.object(mode.ctypes,'CDLL',return_value=libc):
   self.assertEqual(mode.runtime_mode('/org/bluez/hci1'),{'supported':True,'enabled':True})
  sock.sendall.assert_called_once_with(struct.pack('<HHH',4,1,0));sock.close.assert_called_once()
 def test_permission_denied_is_unknown_without_radio_writes(self):
  with patch.object(mode.socket,'socket',side_effect=PermissionError):self.assertIsNone(mode.runtime_mode('/org/bluez/hci1'))
  self.assertIsNone(mode.runtime_mode('/org/bluez/not-an-adapter'))

class ScanGateTests(HeadphoneTests):
 def test_unknown_le_allows_scan_with_accurate_warning(self):
  self.transport.classic_preflight=lambda:('','classic_config_le_unknown')
  self.assertEqual(self.manager.submit('scan')[0],202);self.manager._step()
  self.assertIn('scan',self.transport.calls);self.assertEqual(self.manager.snapshot()['classic_warning'],'classic_config_le_unknown')
 def test_known_disabled_refuses_scan(self):
  self.transport.classic_preflight=lambda:('classic_disabled','')
  self.assertEqual(self.manager.submit('scan')[0],202);self.manager._step()
  self.assertNotIn('scan',self.transport.calls);self.assertEqual(self.manager.snapshot()['scan_error'],'classic_disabled')
 def test_enabled_with_le_config_still_scans_with_warning(self):
  self.transport.classic_preflight=lambda:('','classic_config_le_enabled')
  self.assertEqual(self.manager.submit('scan')[0],202);self.manager._step()
  self.assertIn('scan',self.transport.calls);self.assertEqual(self.manager.snapshot()['classic_warning'],'classic_config_le_enabled')
