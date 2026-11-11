"""Exercise the real dashboard STOP dispatcher and committed-START timeout."""
import ast
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
CONTROL=ROOT/'src/orangepi_collect/control'
LAUNCHER=CONTROL/'launcher.py'
WEB_CONTROLS=CONTROL/'web_controls.py'
sys.path.insert(0,str(ROOT/'src/orangepi_collect/common'))
from scan_registry import RegistryError


def dashboard_tick_method():
    tree=ast.parse(WEB_CONTROLS.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='WebControls')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='dashboard_tick')
    scope={'actions_for':lambda *_:[],'ScanProcessError':RuntimeError,'subprocess':subprocess}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(WEB_CONTROLS),'exec'),scope)
    return scope['dashboard_tick']


def start_methods_type(fake_time):
    tree=ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Controller')
    names={'begin','_begin_after_privacy','_stop_after_start_failure','stop_before_prompt'}
    methods=[node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name in names]
    cls.bases=[];cls.body=methods
    scope={'time':fake_time,'signal':signal,'RegistryError':RegistryError,
        'impedance_allows_recording':lambda *_:True,'fingerprint':lambda _:'fp',
        'devices':lambda:{'auto_discovery':{}},'atomic_json':lambda *_:None}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(LAUNCHER),'exec'),scope)
    return scope['Controller']


class PrivacyGate:
    def __init__(self):
        self.phase='idle';self.committed=False;self.cancelled=False
    def arm(self,*_args,**_kwargs):self.phase='arming';self.committed=False;self.cancelled=False
    def ready(self):return True
    def commit(self):self.committed=True;return {'schema_version':1}
    def stopping(self,*_args,**_kwargs):
        if self.committed:self.phase='stopping'
    def fault(self,_message):
        if self.committed:self.phase='fault'
    def snapshot(self):return {'required':self.committed,'phase':self.phase}


class FakeTime:
    def __init__(self):self.now=0.0
    def monotonic(self):self.now+=.1;return self.now
    def time(self):return 1000.0
    def sleep(self,_seconds):pass


class StopDashboardDispatchTests(unittest.TestCase):
    def dashboard_controller(self,state,valid_target=True):
        target={'instance':'instance-1','session_id':'session-1','nonce':'nonce-1','generation':8}
        privacy=SimpleNamespace(snapshot=Mock(return_value={'required':True,'stop_confirmed':False}),
            target_matches=Mock(return_value=valid_target))
        job={'action':'stop','revision':12,'privacy_target':target}
        dashboard=SimpleNamespace(privacy=privacy,take=Mock(return_value=job),complete=Mock())
        owner=SimpleNamespace(state=state,dashboard=dashboard,ui_revision=12,
            ui_publish=Mock(),ensure_preview=Mock(),voice=SimpleNamespace(tick=Mock(),interrupt=Mock()),
            keyboard=None,selected_devices=[],reference_can_change=Mock(return_value=False),
            stop_before_prompt=Mock(return_value=True),key=Mock(return_value=True),prepare=Mock(),
            OwnerExited=type('OwnerExited',(RuntimeError,),{}))
        return owner,job,target

    def test_dashboard_tick_directly_stops_countdown_error_and_failed_wait(self):
        dispatch=dashboard_tick_method()
        for state in ('countdown','error','failed_wait'):
            with self.subTest(state=state):
                owner,job,target=self.dashboard_controller(state)

                dispatch(owner)

                owner.stop_before_prompt.assert_called_once_with(target)
                owner.key.assert_not_called()
                owner.prepare.assert_not_called()
                owner.dashboard.complete.assert_called_once_with(None)

    def test_dashboard_tick_rejects_old_stop_identity_without_action(self):
        owner,job,_target=self.dashboard_controller('countdown',valid_target=False)

        dashboard_tick_method()(owner)

        owner.dashboard.privacy.target_matches.assert_called_once_with(job['privacy_target'],allow_same_session=True)
        owner.stop_before_prompt.assert_not_called()
        owner.key.assert_not_called()
        owner.prepare.assert_not_called()
        owner.dashboard.complete.assert_called_once()
        self.assertIn('会话已变化',owner.dashboard.complete.call_args.args[0])

    def test_committed_start_confirmation_timeout_sends_stop_and_keeps_privacy_required(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        gate=PrivacyGate();fake_time=FakeTime()
        owner_type=start_methods_type(fake_time);owner=owner_type()
        owner.dashboard=SimpleNamespace(privacy=gate,camera_checks=None)
        owner.voice=SimpleNamespace(interrupt=Mock(),say=Mock(),tick=Mock())
        owner.worker=SimpleNamespace(pid=123,poll=Mock(return_value=None))
        owner.worker_status=Mock(return_value={'state':'ready','pid':456})
        owner.required_capture_error=Mock(return_value=None)
        owner.camera_registry=SimpleNamespace(verify_owner=Mock())
        owner.capture_owner_run='capture-run';owner.path=Path(temp.name)
        owner.selected_devices=[];owner.usb_fingerprint='fp';owner.quit=False
        owner.state='ready';owner.state_write=Mock();owner.ui_publish=Mock();owner.command=Mock()
        owner.keyboard=None;owner.last_help=[]
        owner.stop_pending=False;owner.stop_force_deadline=None

        result=owner.begin()

        self.assertFalse(result)
        self.assertEqual([call.args[0] for call in owner.command.call_args_list],['START','STOP'])
        self.assertTrue(gate.committed)
        self.assertIn(gate.phase,('stopping','fault'))
        self.assertTrue(gate.snapshot()['required'])
        self.assertTrue(owner.stop_pending)
        self.assertEqual(owner.state,'error')
        self.assertIn('开始采集确认超时',owner.last_help[0])


if __name__=='__main__':unittest.main()
