"""Data-host policy: no power writes, no active-capture reset, bounded reconnect."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import tempfile
import unittest
import io
import json
from unittest.mock import Mock,patch

path=Path(__file__).resolve().parents[2]/'services/usb-phone-link/usb_data_host.py'
spec=importlib.util.spec_from_file_location('data_host',path)
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)

class Guard:
    def __init__(self,allowed=True):self.safe=allowed;self.calls=0;self.leased=False
    def allowed(self):self.calls+=1;return self.safe,'fake_safe' if self.safe else 'recording'
    @contextmanager
    def lease(self):
        self.leased=True
        try:yield
        finally:self.leased=False

class Tests(unittest.TestCase):
    def setup_manager(self,partner=True,mode='device',data='device',children=None,allowed=True):
        value=dict(port_present=True,partner=partner,key=(1,1) if partner else None,
                   data_role=data,power_role='sink',driver_mode=mode,host_children=children or [])
        guard=Guard(allowed);writes=[];clock=[100.]
        def writer(path,role):
            self.assertTrue(guard.leased);writes.append((Path(path).as_posix(),role))
            value['driver_mode']='host'
            if value['partner']:value['data_role']='host'
        manager=host.DataHost(guard=guard,clock=lambda:clock[0],writer=writer,snapshot=lambda:dict(value))
        return manager,value,guard,writes,clock

    def test_connected_device_uses_standard_data_swap_only(self):
        manager,value,guard,writes,_=self.setup_manager()
        result=manager.ensure()
        self.assertEqual(result['state'],'host');self.assertEqual(guard.calls,2)
        self.assertTrue(writes[0][0].endswith('port0/data_role'));self.assertEqual(writes[0][1],'host')
        self.assertEqual(value['power_role'],'sink');self.assertFalse(result['power_role_written'])

    def test_already_host_never_restarts_or_writes(self):
        manager,_,guard,writes,_=self.setup_manager(mode='host',data='host',allowed=False)
        self.assertEqual(manager.ensure()['state'],'host');self.assertEqual(writes,[]);self.assertEqual(guard.calls,0)

    def test_recording_blocks_role_swap(self):
        manager,_,_,writes,_=self.setup_manager(allowed=False)
        self.assertEqual(manager.ensure()['state'],'deferred');self.assertEqual(writes,[])

    def test_second_status_check_blocks_newly_started_capture(self):
        manager,_,guard,writes,_=self.setup_manager()
        answers=iter([(True,'safe'),(False,'recording')]);guard.allowed=lambda:next(answers)
        self.assertEqual(manager.ensure()['state'],'deferred');self.assertEqual(writes,[])

    def test_other_downstream_devices_are_not_reset(self):
        manager,_,_,writes,_=self.setup_manager(children=['7-1.2'])
        self.assertEqual(manager.ensure()['state'],'topology_busy');self.assertEqual(writes,[])

    def test_hosting_an_existing_hub_does_not_swap_stale_typec_label(self):
        manager,_,_,writes,_=self.setup_manager(mode='host',data='device',children=['7-1'])
        self.assertEqual(manager.ensure()['state'],'host');self.assertEqual(writes,[])

    def test_unattached_empty_controller_can_initialize_driver_host(self):
        manager,_,_,writes,_=self.setup_manager(partner=False)
        self.assertEqual(manager.ensure()['state'],'host')
        self.assertTrue(writes[0][0].endswith('fc000000.usb/mode'))

    def test_connected_partner_never_uses_debug_force_host(self):
        manager,value,_,writes,_=self.setup_manager(data='host',mode='device')
        self.assertEqual(manager.ensure()['state'],'unsupported');self.assertEqual(writes,[])

    def test_rejected_requests_continue_at_capped_backoff(self):
        manager,_,_,writes,clock=self.setup_manager()
        def reject(path,value):writes.append(str(path));raise OSError(95,'not supported')
        manager.writer=reject
        self.assertEqual(manager.ensure()['state'],'partner_refused')
        self.assertEqual(manager.ensure()['state'],'waiting_retry');self.assertEqual(len(writes),1)
        clock[0]+=11;manager.ensure();clock[0]+=31;manager.ensure();clock[0]+=100;manager.ensure()
        self.assertEqual(len(writes),4)
        manager.ensure();self.assertEqual(len(writes),4)
        clock[0]+=61;manager.ensure();self.assertEqual(len(writes),5)

    def test_replug_resets_attempt_limit_and_retries_new_partner(self):
        manager,value,_,writes,clock=self.setup_manager()
        def reject(path,role):writes.append(str(path));raise OSError(95,'not supported')
        manager.writer=reject
        for delay in [0,11,31]:clock[0]+=delay;manager.ensure()
        value['key']=(2,2);manager.ensure();self.assertEqual(len(writes),4)

    def test_state_file_is_atomic_and_contains_no_capability(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.json';host.save_state({'state':'host','power_role_written':False},path)
            self.assertIn('"state": "host"',path.read_text())
            self.assertEqual(len(list(path.parent.iterdir())),1)

    def test_privacy_and_busy_states_are_refused(self):
        idle=dict(state='device_confirm',privacy=dict(required=False,phase='idle',stop_confirmed=True))
        self.assertTrue(host.safe_snapshot(idle)[0])
        for value in [dict(idle,state='recording'),dict(idle,state='starting'),dict(idle,busy=True),dict(idle,session='/take'),dict(idle,worker={'recording_active':True}),dict(idle,privacy={'required':True}),dict(idle,privacy='bad')]:
            self.assertFalse(host.safe_snapshot(value)[0])

    def test_installer_always_includes_host_policy_and_three_ordering_dropins(self):
        root=Path(__file__).resolve().parents[2]
        source=root/'scripts/install_pi_services.py'
        spec=importlib.util.spec_from_file_location('host_install_plan',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        plan=module.plan(root,False)
        self.assertTrue(any(target.name=='headring-usb-data-host' for _,target,_ in plan))
        self.assertTrue(any(target.name=='headring-usb-data-host.service' for _,target,_ in plan))
        dropins=[target.parent.name for _,target,_ in plan if target.name=='95-usb-data-host.conf']
        self.assertEqual(set(dropins),{'camd.service.d','embodied-pi-collect.service.d','usb-phone-link.service.d'})

    def test_live_idle_ui_does_not_override_recording_camera(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);(base/'runtime').mkdir();(base/'runtime/dashboard_token').write_text('a'*32)
            guard=host.CaptureGuard(base);guard.owner_active=Mock(return_value=False)
            guard.camera_idle=Mock(return_value=(False,'camera_not_idle'))
            value=dict(state='device_confirm',privacy=dict(required=False,phase='idle',stop_confirmed=True))
            with patch.object(host.urllib.request,'urlopen',return_value=io.StringIO(json.dumps(value))):
                self.assertEqual(guard.allowed(),(False,'camera_not_idle'))

    def test_broken_live_ui_never_uses_boot_fallback(self):
        guard=host.CaptureGuard('/unused');guard.owner_active=Mock(return_value=True);guard.camera_idle=Mock()
        with patch.object(Path,'read_text',side_effect=OSError('no status')):
            self.assertEqual(guard.allowed(),(False,'application_active_without_authoritative_status'))
        guard.camera_idle.assert_not_called()

    @unittest.skipIf(host.fcntl is None,'Requires Linux flock')
    def test_profile_capture_leases_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)/'orangepi';(base/'runtime').mkdir(parents=True)
            (base.parent/'profiles/eeg/orangepi/runtime').mkdir(parents=True)
            first=host.CaptureGuard(base);second=host.CaptureGuard(base)
            with first.lease():
                with self.assertRaises(BlockingIOError):
                    with second.lease():pass

if __name__=='__main__':unittest.main(verbosity=2)
