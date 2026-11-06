"""Boot-page availability, camera gating and same-port dashboard handoff."""
import json
import os
from pathlib import Path
import socket
import runpy
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/orangepi_collect/control'))
import startup_status
import service_entry


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)

    def tearDown(self):
        startup_status.close()
        self.tmp.cleanup()

    def begin(self):
        port=startup_status.begin(self.base,{'host':'127.0.0.1','port':0})
        return 'http://127.0.0.1:'+str(port)

    def test_phone_page_and_progress_are_available_before_camera(self):
        url=self.begin()
        startup_status.phase('waiting_camera','正在初始化头环相机。')
        with urlopen(url+'/phone') as response:
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers['X-HeadRing-Startup'],'1')
            self.assertIn('头环正在启动',response.read().decode())
        with urlopen(url+'/api/startup') as response:
            state=json.load(response)
            self.assertEqual(state['phase'],'waiting_camera')
            self.assertGreaterEqual(state['elapsed_seconds'],0)
            self.assertNotIn('actions',state)

    def test_recording_actions_fail_closed_during_boot(self):
        url=self.begin()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url+'/api/action',b'{"action":"start"}',headers={'Content-Type':'application/json'}))
        self.assertEqual(error.exception.code,503)
        self.assertEqual(json.load(error.exception)['error'],'not_ready')

    def test_application_preparation_does_not_bypass_clock_order_gate(self):
        entered=threading.Event();release=threading.Event();launched=threading.Event()
        def wait():entered.set();self.assertTrue(release.wait(3))
        thread=threading.Thread(target=lambda:service_entry.run(self.base,{'enabled':True,'host':'127.0.0.1','port':0},wait,launched.set))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            state=json.loads((self.base/'runtime/startup_status.json').read_text())
            with urlopen('http://127.0.0.1:'+str(state['port'])+'/phone') as response:self.assertEqual(response.status,200)
            self.assertFalse(launched.is_set())
            release.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertTrue(launched.is_set())
        finally:release.set();thread.join(3)

    def test_camera_socket_does_not_bypass_camera_service_state(self):
        now=[0.0]
        socket_path=Mock();socket_path.is_socket.return_value=True
        def state(unit):
            if unit=='camd.service':return 'activating' if now[0]<.4 else 'active'
            return 'inactive'
        def sleep(duration):now[0]+=duration
        service_entry.wait_for_hardware(timeout=1,camera_socket=socket_path,state=state,clock=lambda:now[0],sleep=sleep)
        self.assertAlmostEqual(now[0],.4)

    def test_genuine_camera_failure_times_out_without_launching(self):
        now=[0.0]
        socket_path=Mock();socket_path.is_socket.return_value=False
        with self.assertRaises(TimeoutError):
            service_entry.wait_for_hardware(timeout=.5,camera_socket=socket_path,state=lambda _:'inactive',clock=lambda:now[0],sleep=lambda d:now.__setitem__(0,now[0]+d))

    def test_factory_camera_owner_is_not_bypassed(self):
        with self.assertRaisesRegex(RuntimeError,'another camera owner'):
            service_entry.wait_for_hardware(state=lambda _:'active')

    def test_preparation_can_overlap_camera_initialization_after_clock_barrier(self):
        now=[0.0]
        def state(unit):
            if unit=='egorecord.service':return 'inactive'
            return 'inactive'if now[0]<.4 else 'activating'
        service_entry.wait_for_camera_start(timeout=1,state=state,clock=lambda:now[0],sleep=lambda d:now.__setitem__(0,now[0]+d))
        self.assertAlmostEqual(now[0],.4)

    def test_boot_page_stays_available_until_handoff_guard_finishes(self):
        url=self.begin();entered=threading.Event();release=threading.Event()
        def guard():entered.set();self.assertTrue(release.wait(3))
        startup_status.guard_handoff(guard)
        thread=threading.Thread(target=startup_status.handoff);thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with urlopen(url+'/phone')as response:self.assertEqual(response.status,200)
        finally:release.set();thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_stop_during_camera_wait_does_not_wait_for_camera(self):
        self.begin();guard=Mock(side_effect=RuntimeError('Must not be called during stop'))
        startup_status.guard_handoff(guard)
        startup_status.close();guard.assert_not_called()

    def test_original_launcher_bootstrap_is_one_shot_and_keeps_legacy_entry(self):
        with patch.dict(os.environ,{'ORANGEPI_EARLY_WEB':'1'}),patch.object(service_entry,'main',side_effect=SystemExit(17))as boot:
            with self.assertRaises(SystemExit)as error:
                runpy.run_path(str(ROOT/'src/orangepi_collect/control/launcher.py'),run_name='__main__')
            self.assertEqual(error.exception.code,17)
            boot.assert_called_once()
            self.assertNotIn('ORANGEPI_EARLY_WEB',os.environ)

    def test_existing_listener_is_preserved(self):
        url=self.begin()
        port=int(url.rsplit(':',1)[1])
        with self.assertRaises((OSError,RuntimeError)):
            startup_status.begin(self.base,{'host':'127.0.0.1','port':port})
        with urlopen(url+'/phone') as response:self.assertEqual(response.status,200)

    def test_real_dashboard_takes_over_same_port_and_keeps_authentication(self):
        sys.path.insert(0,str(ROOT/'src/orangepi_collect/common'))
        import project_paths
        import dashboard
        url=self.begin();port=int(url.rsplit(':',1)[1])
        ui=dashboard.Dashboard(self.base,self.base,{'host':'127.0.0.1','port':port})
        try:
            with self.assertRaises(HTTPError) as error:urlopen(url+'/api/status')
            self.assertEqual(error.exception.code,401)
            request=Request(url+'/api/status',headers={'X-Access-Token':ui.token})
            with urlopen(request) as response:
                self.assertEqual(response.status,200)
                self.assertIsNone(response.headers.get('X-HeadRing-Startup'))
            saved=json.loads((self.base/'runtime/startup_status.json').read_text())
            self.assertEqual(saved['phase'],'dashboard_ready')
        finally:ui.close()


if __name__=='__main__':unittest.main()
