import importlib.util
import pathlib
import unittest
from unittest.mock import patch

SRC=pathlib.Path(__file__).resolve().parents[1]/'system/usb_phone_link.py'
spec=importlib.util.spec_from_file_location('link',SRC);u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
def row(serial='phone-a',state='device',transport='1'):
    return dict(serial=serial,state=state,usb='7-1.3.3' if serial=='phone-a' else '7-1.3.4',transport_id=transport)

class Tests(unittest.TestCase):
    def setUp(self):
        self.now=0;self.calls=[];self.maps={}
        def adb(args,timeout=6):
            self.calls.append(args)
            if args[0]=='-s' and args[2:]==['reverse','--list']:
                return 'UsbFfs tcp:8765 tcp:8765' if self.maps.get(args[1]) else ''
            if args[0]=='-s' and args[2:]==['reverse','tcp:8765','tcp:8765']:self.maps[args[1]]=True
            return ''
        for p in [patch.object(u,'adb',side_effect=adb),patch.object(u,'pi_id',return_value='cp_test'),
                  patch.object(u,'transport_key',side_effect=lambda r:(r['serial'],r['usb'],r['transport_id'])),
                  patch.object(u,'launch_browser',return_value=True),patch.object(u,'data_role_state',return_value={})]:
            p.start();self.addCleanup(p.stop)
        self.manager=u.PhoneMonitor(clock=lambda:self.now)
    def test_any_authorized_phone_without_binding_file(self):
        with patch.object(u.CONFIG.__class__,'read_text',side_effect=AssertionError('no binding reads')):
            state=self.manager.refresh([row('unbound-new-phone')])
        self.assertTrue(state['connected']);self.assertFalse(state['binding_required'])
    def test_two_phones_each_get_mapping(self):
        state=self.manager.refresh([row(),row('phone-b')]);self.assertEqual(state['connected_count'],2)
        self.assertEqual(set(self.maps),{'phone-a','phone-b'})
    def test_unauthorized_phone_never_gets_mapping_or_reconnect(self):
        self.manager.refresh([row(state='unauthorized')]);self.now=120
        state=self.manager.refresh([row(state='unauthorized')]);self.assertEqual(state['state'],'authorization_required')
        self.assertEqual(self.calls,[])
    def test_missing_mapping_restored_without_touching_other_ports(self):
        self.manager.refresh([row()]);self.maps.clear();self.manager.refresh([row()])
        sets=[c for c in self.calls if c[2:]==['reverse','tcp:8765','tcp:8765']]
        self.assertEqual(len(sets),2);self.assertFalse(any('--remove-all' in c for c in self.calls))
    def test_transport_id_change_rebinds_even_if_list_looks_present(self):
        self.manager.refresh([row()]);self.manager.refresh([row(transport='2')])
        self.assertEqual(sum(c[2:]==['reverse','tcp:8765','tcp:8765'] for c in self.calls),2)
    def test_reconnect_after_absence_rebinds(self):
        self.manager.refresh([row()]);self.now=1;self.manager.refresh([])
        self.now=5;self.manager.refresh([row()])
        self.assertEqual(sum(c[2:]==['reverse','tcp:8765','tcp:8765'] for c in self.calls),2)
    def test_offline_peer_recovery_scoped_and_rate_limited(self):
        self.manager.refresh([row(state='offline')]);self.now=9;self.manager.refresh([row(state='offline')])
        self.now=10;self.manager.refresh([row(state='offline')])
        self.assertEqual(self.calls,[['-s','phone-a','reconnect']])
    def test_physical_but_missing_adb_recovers_only_when_idle(self):
        physical=[row()]
        with patch.object(u,'service_idle',return_value=False):
            self.manager.refresh([],physical);self.now=31;self.manager.refresh([],physical)
        self.assertEqual(self.calls,[])
        with patch.object(u,'service_idle',return_value=True):
            self.now=92;self.manager.refresh([],physical)
        self.assertEqual(self.calls,[['kill-server'],['start-server']])
    def test_server_never_restarted_with_any_reported_peer(self):
        with patch.object(u,'service_idle',return_value=True):
            self.manager.refresh([], [row()]);self.now=100
            self.manager.refresh([row(state='unauthorized')],[row()])
        self.assertFalse(any(c[0]=='kill-server' for c in self.calls))
    def test_offline_missing_flapping_does_not_reset_recovery_deadline(self):
        with patch.object(u,'service_idle',return_value=True):
            for t in range(32):
                self.now=t
                self.manager.refresh([row(state='offline')] if t%2 else [],[row()])
        self.assertIn(['-s','phone-a','reconnect'],self.calls)
        self.assertIn(['kill-server'],self.calls)
    def test_flapping_recovery_never_restarts_server_while_recording(self):
        with patch.object(u,'service_idle',return_value=False):
            for t in range(32):
                self.now=t
                self.manager.refresh([row(state='offline')] if t%2 else [],[row()])
        self.assertNotIn(['kill-server'],self.calls)
    def test_browser_opens_once_and_not_on_quick_reconnect(self):
        self.manager.refresh([row()]);self.now=4;self.manager.refresh([row()]);self.assertEqual(u.launch_browser.call_count,1)
        self.now=5;self.manager.refresh([]);self.now=9;self.manager.refresh([row(transport='2')]);self.now=13;self.manager.refresh([row(transport='2')])
        self.assertEqual(u.launch_browser.call_count,1)
    def test_wireless_adb_and_emulator_are_excluded(self):
        text='List of devices attached\nphone-a device usb:7-1.3.3 transport_id:5\nemulator-5554 device transport_id:1\n192.168.1.3:5555 device transport_id:2\n'
        with patch.object(u,'adb',return_value=text):rows=u.devices()
        self.assertEqual(rows,[row(transport='5')])
    def test_one_failed_peer_does_not_block_another(self):
        original=u.adb.side_effect
        def fail(args,timeout=6):
            if args[:2]==['-s','phone-a']:raise RuntimeError('lost peer')
            return original(args,timeout)
        with patch.object(u,'adb',side_effect=fail):state=self.manager.refresh([row(),row('phone-b')])
        self.assertEqual(state['connected_count'],1)
        self.assertEqual(state['devices'][0]['state'],'transport_error')

if __name__=='__main__':unittest.main(verbosity=2)
