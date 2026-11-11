"""Disk-failure STOP recovery must publish the actual display snapshot."""
import ast
import builtins
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
CONTROL=ROOT/'src/orangepi_collect/control'
LAUNCHER=CONTROL/'launcher.py'
WEB_CONTROLS=CONTROL/'web_controls.py'
DASHBOARD=CONTROL/'dashboard.py'
COMMON=ROOT/'src/orangepi_collect/common'
sys.path.insert(0,str(COMMON))
from scan_registry import boot_id,group_exists,proc_identity
from screen_privacy import ScreenPrivacy


def controller_state_writer(base):
    tree=ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Controller')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='state_write')
    def fail_disk(*_args,**_kwargs):raise OSError('injected controller state disk failure')
    scope={'os':os,'json':json,'time':time,'BASE':base,'CODE_BASE':base,
        'COLLECTION_MODE':'emg','COLLECTION_CONTEXT':'emg:3',
        'active_session':lambda owner:owner.path.name if owner.path else None,
        'atomic_json':fail_disk}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(LAUNCHER),'exec'),scope)
    return scope['state_write']


def ui_publish_method(base):
    tree=ast.parse(WEB_CONTROLS.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='WebControls')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='ui_publish')
    scope={'json':json,'time':time,'Path':Path,'BASE':base,'PRESETS':(),
        'impedance_presentation':lambda *_args,**_kwargs:{},
        'active_session':lambda owner:owner.path.name if owner.path else None,
        'shutil':shutil}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(WEB_CONTROLS),'exec'),scope)
    return scope['ui_publish']


def dashboard_methods():
    tree=ast.parse(DASHBOARD.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Dashboard')
    methods=[node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name in ('publish','display_state')]
    scope={'copy':copy,'time':time}
    exec(compile(ast.Module(body=methods,type_ignores=[]),str(DASHBOARD),'exec'),scope)
    return scope['publish'],scope['display_state']


def frontend_safe_idle(snapshot):
    html=(CONTROL/'dashboard.html').read_text(encoding='utf-8')
    policy_line=next(line.strip() for line in html.splitlines() if 'const POLICY=' in line)
    start=html.index('  function valid(p){')
    end=html.index('  async function heartbeat',start)
    script=policy_line+'\n'+html[start:end]+'\nconsole.log(safeIdle(JSON.parse(process.argv[1])));'
    result=subprocess.run(['node','-e',script,json.dumps(snapshot)],capture_output=True,text=True,check=True)
    return result.stdout.strip()=='true'


def privacy_for_process(session,process,nonce='privacy-nonce'):
    identity=proc_identity(process.pid)
    owner=proc_identity(os.getpid())
    if identity is None or owner is None:raise AssertionError('test process identity unavailable')
    privacy=ScreenPrivacy();privacy.committed=True;privacy.phase='fault';privacy.nonce=nonce
    privacy.session=str(session.resolve());privacy.committed_ns=time.monotonic_ns()-1
    run='trusted-capture-run'
    start=dict(schema_version=1,policy='screen_black_before_capture_v1',nonce=nonce,
        session_id=privacy.session,controller_pid=os.getpid(),controller_start_ticks=owner['start'],
        boot_id=boot_id(),committed_monotonic_ns=privacy.committed_ns,
        privacy_instance=privacy.instance,privacy_generation=9,owner_run=run,
        worker_pid=process.pid,worker_start_ticks=identity['start'],
        adapter_pid=process.pid,adapter_start_ticks=identity['start'])
    privacy.start_proof=start
    proof=dict(schema_version=1,policy='capture-owner-group-exit-v1',
        session_id=privacy.session,nonce=nonce,privacy_instance=privacy.instance,
        privacy_generation=9,owner_run=run,screen_privacy=start,
        registry_run=run,registry_nonce='registry-nonce',registry_kind='capture',
        registry_finish_verified=False,registry_liveness_verified=True,
        registry_cleanup_confirmed=False,group_absent=True,adapter_reaped=True,
        boot_id=start['boot_id'],controller_pid=os.getpid(),
        controller_start_ticks=owner['start'],adapter_pid=process.pid,
        adapter_start_ticks=identity['start'],worker_pid=process.pid,
        worker_start_ticks=identity['start'],pgid=process.pid,sid=process.pid,
        native_shutdown_confirmed=False,native_qc_success=False,
        observed_monotonic_ns=time.monotonic_ns())
    return privacy,proof


class StopStatePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);(self.base/'runtime').mkdir()
        self.session=self.base/'session';self.session.mkdir()
        self.state_write=controller_state_writer(self.base)
        self.ui_publish=ui_publish_method(self.base)
        self.dashboard_publish,self.display_state=dashboard_methods()
        self.modules={
            'project_paths':ModuleType('project_paths'),
            'qc_deferred':ModuleType('qc_deferred')}
        self.modules['project_paths'].COLLECTION_MODE='emg'
        self.modules['project_paths'].COLLECTION_CONTEXT='emg:3'
        self.modules['qc_deferred'].load=lambda _base:{}

    def dashboard(self,privacy):
        dashboard=SimpleNamespace(lock=threading.Lock(),privacy=privacy,
            snapshot={'state':'recording','session':'old-session','revision':1,
                'worker':{'state':'recording','recording_active':True}},
            busy=False,pending=None,result=None,demands=SimpleNamespace(state=Mock()),review_root=None)
        dashboard.publish=MethodType(self.dashboard_publish,dashboard)
        dashboard.display_state=MethodType(self.display_state,dashboard)
        return dashboard

    def controller(self,dashboard,state,worker=None,worker_data=None,owner_exit_recovered=False):
        owner=SimpleNamespace(dashboard=dashboard,state=state,worker=worker,path=self.session,
            voice=SimpleNamespace(realtime=False,silent=False,last_text=''),quit=False,
            ui_revision=1,ui_last_revision=-1,ui_last_disk=time.monotonic(),ui_storage={},
            image=None,success_count=0,selected_image=None,selected_devices=[],
            wrist_wizard=None,emg_wizard=None,web_config={},last_scan=[],last_help=[],
            owner_exit_recovered=owner_exit_recovered,worker_status=Mock(return_value=worker_data or {}),
            headband_calibration_info=lambda _worker:{'warnings':[]},
            audio_status=lambda:{'warning':''},wrist_settings_status=lambda:{})
        owner.ui_publish=MethodType(self.ui_publish,owner)
        return owner

    def display_after_state_write(self,owner):
        with unittest.mock.patch.dict(sys.modules,self.modules):
            self.state_write(owner)
        return owner.dashboard.display_state()

    def test_owner_exit_disk_error_publishes_failed_idle_snapshot(self):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.1)'],start_new_session=True)
        try:
            privacy,proof=privacy_for_process(self.session,process)
            process.wait(timeout=3)
            self.assertFalse(group_exists(process.pid))
            self.assertTrue(privacy.confirm_owner_exited(proof))
            dashboard=self.dashboard(privacy)
            owner=self.controller(dashboard,'error',owner_exit_recovered=True)

            snapshot=self.display_after_state_write(owner)

            self.assertEqual(snapshot['state'],'error')
            self.assertEqual(snapshot['privacy']['phase'],'idle')
            self.assertFalse(snapshot['privacy']['required'])
            self.assertTrue(snapshot['privacy']['stop_confirmed'])
            self.assertIsNot(snapshot['worker'].get('recording_active'),True)
            # These are the real dashboard.html safeIdle prerequisites.
            self.assertNotIn(snapshot['state'],('recording','countdown'))
            self.assertEqual(owner.owner_exit_persist_error,'injected controller state disk failure')
            self.assertTrue(frontend_safe_idle(snapshot))
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)

    def test_owner_exit_disk_and_publish_errors_use_dashboard_fallback(self):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.1)'],start_new_session=True)
        try:
            privacy,proof=privacy_for_process(self.session,process)
            process.wait(timeout=3)
            self.assertTrue(privacy.confirm_owner_exited(proof))
            dashboard=self.dashboard(privacy)
            owner=self.controller(dashboard,'error',owner_exit_recovered=True)
            owner.ui_publish=lambda **_kwargs:(_ for _ in ()).throw(OSError('injected UI publisher failure'))

            snapshot=self.display_after_state_write(owner)

            self.assertEqual(snapshot['state'],'error')
            self.assertFalse(snapshot['privacy']['required'])
            self.assertTrue(snapshot['privacy']['stop_confirmed'])
            self.assertIs(snapshot['worker']['recording_active'],False)
            self.assertNotIn(snapshot['state'],('recording','countdown'))
            self.assertTrue(frontend_safe_idle(snapshot))
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)

    def test_live_or_mismatched_owner_keeps_display_locked_after_disk_error(self):
        live=subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'],start_new_session=True)
        try:
            privacy,proof=privacy_for_process(self.session,live)
            self.assertFalse(privacy.confirm_owner_exited(proof))
            dashboard=self.dashboard(privacy)
            owner=self.controller(dashboard,'recording',worker=live,
                worker_data={'state':'recording','recording_active':True})
            with self.assertRaisesRegex(OSError,'injected controller state disk failure'):
                self.display_after_state_write(owner)
            snapshot=dashboard.display_state()
            self.assertEqual(snapshot['state'],'recording')
            self.assertTrue(snapshot['privacy']['required'])
            self.assertTrue(snapshot['worker']['recording_active'])
            self.assertFalse(frontend_safe_idle(snapshot))
        finally:
            if live.poll() is None:live.kill();live.wait(timeout=3)

        exited=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.1)'],start_new_session=True)
        try:
            privacy,proof=privacy_for_process(self.session,exited)
            exited.wait(timeout=3)
            self.assertFalse(privacy.confirm_owner_exited(dict(proof,nonce='wrong-session')))
            dashboard=self.dashboard(privacy)
            owner=self.controller(dashboard,'error',owner_exit_recovered=False)
            with self.assertRaisesRegex(OSError,'injected controller state disk failure'):
                self.display_after_state_write(owner)
            snapshot=dashboard.display_state()
            self.assertEqual(snapshot['state'],'error')
            self.assertTrue(snapshot['privacy']['required'])
            self.assertIsNot(snapshot['worker']['recording_active'],True)
            self.assertFalse(frontend_safe_idle(snapshot))
        finally:
            if exited.poll() is None:exited.kill();exited.wait(timeout=3)


if __name__=='__main__':unittest.main()
