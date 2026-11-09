import io,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'services/phone-media'))
sys.path.insert(0,str(ROOT/'orangepi/tests'))
sys.path.insert(0,str(ROOT/'orangepi/src/orangepi_collect/control'))
import capture_gate as gate
import headphone_manager as module
import headphone_http
import phone_http
from test_headphone_manager import Transport,MAC

SAFE={'state':'idle','busy':False,'session':None,'revision':4,'collection_context':'emg:3','privacy':{'required':False,'stop_confirmed':True,'phase':'idle','instance':'test-instance','generation':7},'worker':{'recording_active':False,'state':'idle'}}
class Checks(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);(self.base/'configs').mkdir()
  self.generation=('emg:3',4,'test-instance',7);self.current=self.generation;self.t=Transport();self.t.stop_scan=Mock()
  self.m=module.HeadphoneManager(self.base,self.t,start=False,capture_gate=lambda:self.current)
  self.m._load();self.m._refresh()
 def test_bluez_in_progress_is_reported_as_scan_busy_only(self):
  error=SimpleNamespace(get_dbus_name=lambda:'org.bluez.Error.InProgress')
  self.assertEqual(module.BluezTransport._error(error,'scan_failed').code,'scan_busy')
  self.assertEqual(module.BluezTransport._error(error,'connect_failed').code,'connect_failed')
 def test_scan_busy_survives_connected_headphone_refresh_until_next_action(self):
  self.m.selected=MAC;self.t.paired=True;self.t.trusted=True;self.t.connected=True;self.m._refresh()
  self.t.start_scan=Mock(side_effect=module.BluetoothError('scan_busy'))
  self.assertEqual(self.m.submit('scan')[0],202);self.m._step();self.assertEqual(self.m.snapshot()['error'],'scan_busy')
  self.m._step();self.assertEqual(self.m.snapshot()['error'],'scan_busy')
  self.t.start_scan=Mock();self.assertEqual(self.m.submit('scan')[0],202);self.assertEqual(self.m.snapshot()['error'],'')
 def test_scan_busy_survives_automatic_reconnect_success_and_failure(self):
  for succeeds in (True,False):
   with self.subTest(succeeds=succeeds):
    now=[100.0];self.m.clock=lambda:now[0];self.m.selected=MAC
    self.t.connected=False;self.t.paired=True;self.t.trusted=True;self.m._refresh()
    self.t.start_scan=Mock(side_effect=module.BluetoothError('scan_busy'))
    self.assertEqual(self.m.submit('scan')[0],202);self.m._step()
    self.assertEqual(self.m.snapshot()['scan_error'],'scan_busy')
    self.t.connect=Mock() if succeeds else Mock(side_effect=module.BluetoothError('connect_failed'))
    if succeeds:self.t.connect.side_effect=lambda _:setattr(self.t,'connected',True)
    now[0]+=6;self.m._step()
    self.assertEqual(self.m.snapshot()['scan_error'],'scan_busy')
    if not succeeds:self.assertEqual(self.m.snapshot()['error'],'connect_failed')
    self.t.start_scan=Mock();self.assertEqual(self.m.submit('scan')[0],202)
    self.assertEqual(self.m.snapshot()['scan_error'],'')
    self.m.pending=None;self.m._update(busy=False,error='',scan_error='');self.t.connected=False;self.m.next_retry=0
 def test_missing_unknown_unauthorized_rejected(self):
  self.assertEqual(gate.generation(SAFE),self.generation)
  for patchval in ({'state':'recording'},{'state':'preparing'},{'busy':True},{'worker':None},{'worker':{'state':'unknown'}},{'revision':None},{'privacy':None}):
   self.assertIsNone(gate.generation(dict(SAFE,**patchval)))
  with patch('capture_gate.urllib.request.urlopen',side_effect=OSError('401')):
   self.assertIsNone(gate.CaptureGate('token')())
 def test_rejected_http_is_never_queued(self):
  self.current=None
  h=SimpleNamespace(path='/headphones',headers={'Content-Type':'application/json','Content-Length':'17'},server=SimpleNamespace(token='test',headphones=self.m),client_address=('127.0.0.1',1),rfile=io.BytesIO(b'{"action":"scan"}'),send=Mock())
  h.headers['X-Access-Token']='test'
  self.assertTrue(headphone_http.handle_headphones(h,'POST'));self.assertEqual(h.send.call_args.args[0],423)
  self.assertIsNone(self.m.pending);self.m._step();self.assertNotIn('scan',self.t.calls)
 def test_stale_enqueued_operation_dropped(self):
  self.assertEqual(self.m.submit('scan')[0],202);self.current=('emg:3',5,'test-instance',7)
  self.m._step();self.assertNotIn('scan',self.t.calls);self.assertEqual(self.m.snapshot()['error'],'capture_busy')
 def test_scan_stops_own_discovery_when_generation_changes(self):
  self.assertEqual(self.m.submit('scan')[0],202);self.m._step();self.assertIn('scan',self.t.calls)
  self.current=None;self.m._step();self.t.stop_scan.assert_called_once();self.assertFalse(self.m.snapshot()['scanning'])
 def test_auto_reconnect_is_gated(self):
  self.m.selected=MAC;self.t.paired=True;self.t.trusted=True;self.current=None;self.m._step();self.assertNotIn('connect',self.t.calls)
  self.current=self.generation;self.m.next_retry=0;self.m._step();self.assertIn('connect',self.t.calls)
 def test_inflight_connect_not_pretended_cancelled_or_disconnected(self):
  self.t.paired=True;self.t.trusted=True
  def connect(mac):self.t.calls.append('connect');self.t.connected=True;self.current=None
  self.t.connect=connect
  self.assertEqual(self.m.submit('connect',MAC)[0],202);self.m._step()
  self.assertIn('connect',self.t.calls);self.assertNotIn('disconnect',self.t.calls);self.assertTrue(self.t.connected)
 def test_proxy_423_does_not_forward(self):
  h=SimpleNamespace(path='/api/phone/sidecar/headphones',headers={'Content-Length':'17'},authorized=lambda:True,rfile=io.BytesIO(b'{"action":"scan"}'),send=Mock())
  owner=SimpleNamespace(display_state=Mock(return_value=dict(SAFE,state='recording')),phone_slots=threading.Semaphore(1),token='token')
  with patch.object(phone_http,'HTTPConnection') as connection:
   self.assertTrue(phone_http.handle_phone_request(h,owner,'POST'));self.assertEqual(h.send.call_args.args[0],423);connection.assert_not_called()
 def test_proxy_accepted_action_never_misreports_423(self):
  h=SimpleNamespace(path='/api/phone/sidecar/headphones',headers={'Content-Length':'17'},authorized=lambda:True,rfile=io.BytesIO(b'{"action":"scan"}'),send=Mock())
  owner=SimpleNamespace(display_state=Mock(side_effect=[SAFE,dict(SAFE,state='recording')]),phone_slots=threading.Semaphore(1),token='token')
  reply=Mock(status=202,read=Mock(return_value=b'{}'));conn=Mock(getresponse=Mock(return_value=reply))
  with patch.object(phone_http,'HTTPConnection',return_value=conn):
   self.assertTrue(phone_http.handle_phone_request(h,owner,'POST'));self.assertEqual(h.send.call_args.args[0],202)
 def test_disconnect_profile_only_for_temporary_disconnect(self):
  t=module.BluezTransport();t.paths={MAC:'/dev/fake'};dev=Mock();t._interface=Mock(return_value=dev)
  t.disconnect(MAC,audio_only=True);dev.DisconnectProfile.assert_called_once_with(module.AUDIO_SINK,timeout=5);dev.Disconnect.assert_not_called()
  dev.reset_mock();t.disconnect(MAC,audio_only=False);dev.Disconnect.assert_called_once()
 def test_selected_adapter_preferred(self):
  t=module.BluezTransport();t.preferred_address=MAC
  objs={'/org/bluez/hci0':{'org.bluez.Adapter1':{'Powered':True}},'/org/bluez/hci1':{'org.bluez.Adapter1':{'Powered':True}},'/org/bluez/hci0/dev_old':{'org.bluez.Device1':{'Address':module.LEGACY_ADDRESS,'Adapter':'/org/bluez/hci0','Class':0x400}},'/org/bluez/hci1/dev_new':{'org.bluez.Device1':{'Address':MAC,'Adapter':'/org/bluez/hci1','Class':0x400}}}
  t._interface=Mock(return_value=Mock(GetManagedObjects=Mock(return_value=objs)));r=t.inspect()
  self.assertEqual(t.adapter,'/org/bluez/hci1');self.assertEqual(r['devices'][0]['address'],MAC)

if __name__=='__main__':unittest.main()
