import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'services/phone-media'))
import headphone_manager as module
MAC='00:11:22:33:44:55'

class Transport:
    def __init__(self):self.connected=False;self.paired=False;self.trusted=False;self.calls=[];self.audio=True
    def inspect(self):return {'powered':True,'devices':[dict(address=MAC,name='Test Headset',audio=True,connected=self.connected,paired=self.paired,trusted=self.trusted,battery_percent=60)]}
    def sinks(self):return {module.sink_name(MAC)} if self.connected and self.audio else set()
    def pair(self,mac):self.calls.append('pair');self.paired=True
    def trust(self,mac):self.calls.append('trust');self.trusted=True
    def connect(self,mac):self.calls.append('connect');self.connected=True
    def disconnect(self,mac,audio_only=False):self.calls.append('disconnect');self.connected=False
    def start_scan(self):self.calls.append('scan')
    def stop_scan(self):pass
    def close(self):pass
    def test(self,mac):self.calls.append('test')

class HeadphoneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);(self.base/'configs').mkdir()
        self.transport=Transport();self.manager=module.HeadphoneManager(self.base,self.transport,start=False,capture_gate=lambda:('test',1));self.manager._load()
    def select(self):self.manager._execute('connect',MAC)
    def test_first_explicit_connection_pairs_trusts_and_saves(self):
        self.select();self.assertEqual(self.transport.calls,['pair','trust','connect'])
        self.assertEqual(json.loads(self.manager.selection_path.read_text())['address'],MAC)
        self.assertTrue(self.manager.snapshot()['audio_ready']);self.assertTrue(self.manager.snapshot()['auto_reconnect'])
    def test_saved_headphone_reconnects_after_manager_restart(self):
        self.select();self.transport.connected=False;self.transport.calls=[]
        next_manager=module.HeadphoneManager(self.base,self.transport,start=False,capture_gate=lambda:('test',1));next_manager._load();next_manager._step()
        self.assertEqual(self.transport.calls,['connect']);self.assertTrue(next_manager.snapshot()['audio_ready'])
    def test_temporary_disconnect_keeps_preference_and_pauses_retry(self):
        self.select();self.manager._execute('disconnect',None);self.transport.calls=[];self.manager._step()
        self.assertEqual(json.loads(self.manager.selection_path.read_text())['address'],MAC)
        self.assertEqual(self.transport.calls,[]);self.assertTrue(self.manager.snapshot()['auto_reconnect_paused'])
    def test_forget_clears_automatic_selection(self):
        self.select();self.manager._execute('forget',None);self.manager._refresh()
        self.assertIsNone(json.loads(self.manager.selection_path.read_text())['address']);self.assertFalse(self.manager.snapshot()['auto_reconnect'])
    def test_bluetooth_link_is_not_mistaken_for_ready_audio(self):
        self.select();self.transport.audio=False;self.manager._refresh();state=self.manager.snapshot()
        self.assertTrue(state['bluetooth_connected']);self.assertFalse(state['audio_ready']);self.assertEqual(state['battery_percent'],60)
    def test_non_interactive_reconnect_never_pairs_an_unknown_device(self):
        self.manager._save(MAC)
        with self.assertRaises(module.BluetoothError):self.manager._connect(MAC,manual=False)
        self.assertEqual(self.transport.calls,[])
    def test_pair_uses_a_headless_application_agent_and_verifies_bluez(self):
        transport=module.BluezTransport();transport.inspect=Mock(return_value={'devices':[{'address':MAC,'paired':True}]})
        with patch.object(module.subprocess,'run') as command:transport.pair(MAC)
        args=command.call_args.args[0];self.assertIn('NoInputNoOutput',args);self.assertEqual(args[-2:],['pair',MAC])
        transport.inspect.assert_called_once()
    def test_cli_exit_code_does_not_replace_pairing_state_verification(self):
        transport=module.BluezTransport();transport.inspect=Mock(return_value={'devices':[{'address':MAC,'paired':False}]})
        with patch.object(module.subprocess,'run',return_value=Mock(returncode=0)):
            with self.assertRaises(module.BluetoothError):transport.pair(MAC)
    def test_missing_or_invalid_battery_never_breaks_audio_status(self):
        for value in (None,'not-reported',-1,101):self.assertIsNone(module.battery_percent({'org.bluez.Battery1':{'Percentage':value}}))

if __name__=='__main__':unittest.main(verbosity=2)
