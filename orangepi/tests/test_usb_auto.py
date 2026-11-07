import importlib.util,io,json,pathlib,tempfile,unittest
from unittest.mock import patch
P=pathlib.Path(__file__).resolve().parents[2]/'services/usb-phone-link/usb_phone_link.py'
spec=importlib.util.spec_from_file_location('usb_auto',P);usb=importlib.util.module_from_spec(spec);spec.loader.exec_module(usb)

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.patch=patch.object(usb,'CONFIG',pathlib.Path(self.tmp.name)/'binding.json');self.patch.start();self.addCleanup(self.patch.stop)
  self.row=dict(serial='ANDROID123',state='device',usb='7-1.3')
 def test_only_physical_usb_is_selected(self):
  with patch.object(usb,'adb',return_value='List of devices attached\n192.168.1.10:5555 device product:android\nANDROID123 device usb:7-1.3 product:android\n'):
   self.assertEqual(usb.devices(),[self.row])
 def test_new_phone_needs_no_binding(self):
  self.assertEqual(usb.choose([self.row]),(self.row,None));self.assertFalse(usb.CONFIG.exists())
 def test_multiple_phones_not_chosen_arbitrarily(self):
  self.assertEqual(usb.choose([self.row,dict(self.row,serial='OTHER')]),(None,'multiple_phones'))
 def test_saved_binding_is_preference_only(self):
  usb.CONFIG.write_text(json.dumps(dict(pi_id='cp_0011',phone_serial='ANDROID123')))
  with patch.object(usb,'pi_id',return_value='cp_0011'):
   self.assertEqual(usb.choose([self.row,dict(self.row,serial='OTHER')])[0],self.row)
   self.assertEqual(usb.choose([dict(self.row,serial='OTHER')])[0]['serial'],'OTHER')
 def test_authorization_cannot_be_bypassed(self):
  with patch.object(usb,'devices',return_value=[dict(self.row,state='unauthorized')]),patch.object(usb,'pi_id',return_value='cp_0011'),patch.object(usb,'adb') as adb:
   state,row=usb.once();self.assertIsNone(row);self.assertEqual(state['state'],'authorization_required');adb.assert_not_called()
 def test_reverse_created_once_and_idempotent(self):
  with patch.object(usb,'devices',return_value=[self.row]),patch.object(usb,'pi_id',return_value='cp_0011'),patch.object(usb,'adb',side_effect=['','']) as adb:
   state,row=usb.once();self.assertTrue(state['connected']);self.assertEqual(adb.call_count,2)
  with patch.object(usb,'devices',return_value=[self.row]),patch.object(usb,'pi_id',return_value='cp_0011'),patch.object(usb,'adb',return_value='UsbFfs tcp:8765 tcp:8765') as adb:
   usb.once();self.assertEqual(adb.call_count,1)
 def test_browser_receives_fragment_not_query(self):
  with patch.object(usb,'browser_ready',return_value='A'*32),patch.object(usb,'adb') as adb:
   self.assertTrue(usb.launch_browser(self.row));args=adb.call_args.args[0];self.assertIn('#'+'A'*32,args[-1]);self.assertNotIn('token=',args[-1])
 def test_browser_not_launched_when_not_safe(self):
  with patch.object(usb,'browser_ready',return_value=None),patch.object(usb,'adb') as adb:
   self.assertFalse(usb.launch_browser(self.row));adb.assert_not_called()
 def test_state_file_is_atomic(self):
  path=pathlib.Path(self.tmp.name)/'state.json';usb.atomic(path,dict(connected=True));self.assertTrue(json.loads(path.read_text())['connected']);self.assertEqual(len(list(path.parent.iterdir())),1)
 def test_browser_ready_preserves_recording_and_privacy_guards(self):
  base=pathlib.Path(self.tmp.name);(base/'runtime').mkdir();(base/'runtime/dashboard_token').write_text('A'*32)
  for value in [dict(state='recording'),dict(state='countdown'),dict(state='device_confirm',busy=True),dict(state='device_confirm',privacy=dict(required=True)),dict(state='device_confirm',worker=dict(recording_active=True))]:
   with patch.object(usb,'BASE',base),patch.object(usb.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(value))):
    self.assertIsNone(usb.browser_ready())
 def test_browser_ready_uses_existing_capability_for_idle_page(self):
  base=pathlib.Path(self.tmp.name);(base/'runtime').mkdir();(base/'runtime/dashboard_token').write_text('A'*32)
  with patch.object(usb,'BASE',base),patch.object(usb.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(dict(state='device_confirm',privacy=dict(required=False))))):
   self.assertEqual(usb.browser_ready(),'A'*32)

if __name__=='__main__':unittest.main(verbosity=2)
