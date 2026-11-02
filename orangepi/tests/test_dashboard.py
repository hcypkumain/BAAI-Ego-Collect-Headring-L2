"""Safety and identity regressions for serial roles and browser control."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
import project_paths
import device_discovery
import dashboard
import keyboard_input
import launcher
from test_discovery import config,candidate


class PersistentRolesTests(unittest.TestCase):
    def cfg(self):
        c=config();c['emg_serial_roles']={'emg_left':'5C37269104','emg_right':'5C37268495'};return c

    def test_port_changes_and_wrong_history_cannot_swap_known_serials(self):
        c=self.cfg();l=candidate('emg','9.8','5C37269104');r=candidate('emg','1.1','5C37268495')
        state={'slots':{'emg_left':dict(r,role_confirmed=True),'emg_right':dict(l,role_confirmed=True)}}
        result,_=device_discovery.resolve(c,[r,l],state)
        self.assertEqual(result['emg']['emg_left']['port'],l['path'])
        self.assertEqual(result['emg']['emg_right']['port'],r['path'])
        self.assertTrue(all(v['discovery']['role_confirmed'] for v in result['emg'].values()))

    def test_missing_left_does_not_use_right_or_unknown_receiver(self):
        rows=[candidate('emg','1.1','5C37268495'),candidate('emg','1.2','NEW')]
        result,_=device_discovery.resolve(self.cfg(),rows,{})
        self.assertFalse(result['emg']['emg_left']['discovery']['present'])
        self.assertEqual(result['emg']['emg_right']['port'],rows[0]['path'])

    def test_duplicate_serial_is_not_accepted(self):
        rows=[candidate('emg','1.1','5C37269104'),candidate('emg','1.2','5C37269104')]
        result,_=device_discovery.resolve(self.cfg(),rows,{})
        self.assertFalse(result['emg']['emg_left']['discovery']['present'])
        self.assertEqual(result['emg']['emg_left']['discovery']['reason'],'ambiguous')

    def test_invalid_roles_fail_instead_of_silently_using_ports(self):
        c=self.cfg();c['emg_serial_roles']['emg_right']=c['emg_serial_roles']['emg_left']
        with self.assertRaises(ValueError):device_discovery.resolve(c,[],{})

    def test_swap_survives_fresh_registry_and_new_topology(self):
        c=self.cfg();c['emg_serial_roles']={'emg_left':'5C37268495','emg_right':'5C37269104'}
        rows=[candidate('emg','6.7','5C37269104'),candidate('emg','2.4','5C37268495')]
        result,_=device_discovery.resolve(c,rows,{})
        self.assertEqual(result['emg']['emg_left']['port'],rows[1]['path'])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);(self.base/'runtime').mkdir()
        self.ui=dashboard.Dashboard(self.base,self.base,{'host':'127.0.0.1','port':0})
        self.url='http://127.0.0.1:'+str(self.ui.server.server_port)
        self.ui.publish({'state':'ready','revision':7})
    def tearDown(self):self.ui.close();self.tmp.cleanup()
    def post(self,payload,token=True,origin=None):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Access-Token']=self.ui.token
        if origin:headers['Origin']=origin
        return urlopen(Request(self.url+'/api/action',json.dumps(payload).encode(),headers),timeout=3)
    def test_unauthenticated_reads_and_actions_rejected(self):
        for path in ('/api/status','/api/reference','/preview/cam_left_wrist.jpg'):
            with self.assertRaises(HTTPError) as e:urlopen(self.url+path)
            self.assertEqual(e.exception.code,401)
        with self.assertRaises(HTTPError) as e:self.post({'action':'start','revision':7},False)
        self.assertEqual(e.exception.code,401)
    def without_access_code(self):
        self.ui.close()
        self.ui=dashboard.Dashboard(self.base,self.base,{'host':'127.0.0.1','port':0,'access_code_required':False})
        self.url='http://127.0.0.1:'+str(self.ui.server.server_port)
        self.ui.publish({'state':'ready','revision':7})
    def test_no_code_mode_serves_status_preview_and_page_without_credentials(self):
        self.without_access_code()
        (self.base/'cam_left_wrist.jpg').write_bytes(b'\xff\xd8preview\xff\xd9')
        with urlopen(self.url+'/') as response:
            self.assertIn(b'<body data-access-code-required="false">',response.read())
        with urlopen(self.url+'/api/status') as response:self.assertEqual(json.load(response)['revision'],7)
        with urlopen(self.url+'/preview/cam_left_wrist.jpg') as response:self.assertEqual(response.read(),b'\xff\xd8preview\xff\xd9')
        self.assertEqual(self.post({'action':'start','revision':7},token=False).status,202)
        self.assertEqual(self.ui.take()['action'],'start')
    def test_no_code_mode_still_checks_origin_and_state(self):
        self.without_access_code()
        for payload,origin,expected in (({'action':'start','revision':7},'http://other-host',403),
                                         ({'action':'start','revision':6},None,409),
                                         ({'action':'stop','revision':7},None,409)):
            with self.assertRaises(HTTPError) as e:self.post(payload,token=False,origin=origin)
            self.assertEqual(e.exception.code,expected)
        self.assertIsNone(self.ui.take())
    def test_no_code_mode_video_head_reaches_resource_check(self):
        self.without_access_code()
        with self.assertRaises(HTTPError) as e:urlopen(Request(self.url+'/api/video/cam_left_wrist?session=missing',method='HEAD'))
        self.assertEqual(e.exception.code,404)
    def test_access_code_setting_must_be_boolean(self):
        with self.assertRaises(ValueError):dashboard.Dashboard(self.base,self.base,{'port':0,'access_code_required':'false'})
    def test_stale_click_and_wrong_action_do_not_queue(self):
        for payload in ({'action':'stop','revision':7},{'action':'start','revision':6}):
            with self.assertRaises(HTTPError) as e:self.post(payload)
            self.assertEqual(e.exception.code,409)
        self.assertIsNone(self.ui.take())
    def test_duplicate_start_is_rejected_and_never_becomes_stop(self):
        self.assertEqual(self.post({'action':'start','revision':7}).status,202)
        with self.assertRaises(HTTPError):self.post({'action':'start','revision':7})
        job=self.ui.take();self.assertEqual(job['action'],'start')
        self.ui.publish({'state':'recording','revision':8});self.ui.complete()
        with self.assertRaises(HTTPError):self.post({'action':'start','revision':8})
        self.assertIsNone(self.ui.take())
    def test_cross_site_action_rejected(self):
        with self.assertRaises(HTTPError) as e:self.post({'action':'start','revision':7},origin='http://other-host')
        self.assertEqual(e.exception.code,403)
    def test_volume_is_bounded_and_integer(self):
        for value in (0,121,'110',True):
            with self.assertRaises(HTTPError):self.post({'action':'volume','revision':7,'value':value})
    def test_stale_signal_not_shown_as_live(self):
        (self.base/'emg_left.json').write_text(json.dumps({'time':time.time()-10,'rows':[]}))
        self.assertFalse(self.ui.status()['signals']['emg_left']['live'])
    def test_path_traversal_cannot_read_local_files(self):
        request=Request(self.url+'/preview/../../runtime/dashboard_token',headers={'X-Access-Token':self.ui.token})
        with self.assertRaises(HTTPError) as e:urlopen(request)
        self.assertEqual(e.exception.code,404)
    def test_status_reports_boot_and_process_without_mutating_snapshot(self):
        with patch('dashboard.os.getpid',return_value=1234):
            self.ui.boot_id='boot-for-test'
            status=self.ui.status()
        self.assertEqual(status['system'],{'boot_id':'boot-for-test','controller_pid':1234})
        self.assertNotIn('system',self.ui.snapshot)

    def test_configuration_controls_locked_during_recording(self):
        self.ui.publish({'state':'recording','revision':8})
        self.assertEqual(self.ui.status()['actions'],['stop'])
        with self.assertRaises(HTTPError):self.post({'action':'swap_emg','revision':8})

    def test_wrist_method_and_manual_confirmation_are_state_scoped(self):
        self.ui.publish({'state':'wrist_choose','revision':9})
        for payload in ({'action':'wrist_method','value':'invalid','revision':9},
                        {'action':'assign_wrist_roles','value':{},'revision':9}):
            with self.assertRaises(HTTPError):self.post(payload)
        self.assertIsNone(self.ui.take())
        self.assertEqual(self.post({'action':'wrist_method','value':'manual','revision':9}).status,202)
        self.assertEqual(self.ui.take()['value'],'manual');self.ui.complete()
        self.ui.publish({'state':'wrist_manual','revision':10})
        value={'session_id':'session','preview_id':'preview','left_slot':'cam_right_wrist'}
        self.assertEqual(self.post({'action':'assign_wrist_roles','value':value,'revision':10}).status,202)
        with self.assertRaises(HTTPError):self.post({'action':'assign_wrist_roles','value':value,'revision':10})
        self.assertEqual(self.ui.take()['value'],value)

    def test_manual_confirmation_rejects_raw_device_paths_and_malformed_drafts(self):
        self.ui.publish({'state':'wrist_manual','revision':10})
        for value in (None,{}, {'session_id':'s','preview_id':'p','left_slot':'/dev/video0'},
                      {'session_id':[],'preview_id':'p','left_slot':'cam_left_wrist'},
                      {'session_id':'s','preview_id':'p','left_slot':'cam_left_wrist','device':'/dev/video0'}):
            with self.assertRaises(HTTPError):self.post({'action':'assign_wrist_roles','value':value,'revision':10})
        self.assertIsNone(self.ui.take())

    def test_manual_mapping_cannot_change_during_recording_or_finalizing(self):
        for state in ('recording','countdown','finalizing','confirm_stop'):
            self.ui.publish({'state':state,'revision':11})
            for action in ('wrist_method','assign_wrist_roles'):
                with self.assertRaises(HTTPError):self.post({'action':action,'value':'manual','revision':11})


class KeyboardTests(unittest.TestCase):
    def test_generic_keyboard_capabilities_not_model_name(self):
        mask=sum(1<<i for i in (28,30,48))
        text='N: Name="Other Bluetooth Keyboard"\nH: Handlers=kbd event23\nB: KEY=%x\n'%mask
        self.assertEqual(keyboard_input.keyboards(text),[('/dev/input/event23','Other Bluetooth Keyboard')])
    def test_camera_shutter_and_power_buttons_are_not_keyboards(self):
        text='N: Name="Camera"\nH: Handlers=kbd event3\nB: KEY=100000 0 0 0\n'
        self.assertEqual(keyboard_input.keyboards(text),[])


class OwnershipTests(unittest.TestCase):
    def test_recorder_ownership_prevents_idle_preview(self):
        c=launcher.Controller.__new__(launcher.Controller);c.dashboard=Mock();c.worker=Mock()
        with patch.object(launcher.subprocess,'Popen',side_effect=AssertionError('second reader')):c.ensure_preview()
    def test_reconfiguration_cannot_stop_active_recording(self):
        c=launcher.Controller.__new__(launcher.Controller);c.worker=Mock();c.state='recording'
        c.command=Mock()
        with self.assertRaises(ValueError):c.release_prepared()
        c.command.assert_not_called()

class PreviewTests(unittest.TestCase):
    def test_signal_preserves_signed_extrema_and_bounded_history(self):
        from live_preview import Publisher
        with tempfile.TemporaryDirectory() as directory:
            pub=Publisher(directory)
            try:
                for i in range(150):
                    frame=b'\xd2\xd2\xd2\xaa\x00'+b''.join(n.to_bytes(3,'big',signed=True) for n in [-8388608,-1,0,1,8388607,200,-300,5])
                    with patch('live_preview.time.time',return_value=100+i*.025):pub.emg('emg_left',frame)
                deadline=time.monotonic()+2;path=Path(directory)/'emg_left.json'
                while not path.exists() and time.monotonic()<deadline:time.sleep(.02)
                data=json.loads(path.read_text())
                self.assertEqual(len(data['rows']),100)
                self.assertEqual(data['rows'][-1][1][0],[-8388608,-8388608])
                self.assertEqual(data['count'],150)
            finally:pub.close()

if __name__=='__main__':unittest.main()
