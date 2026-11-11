"""Regression checks for STOP ordering, warning timeout, and failure recovery UI."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
COMMON=ROOT/'src/orangepi_collect/common'
sys.path.insert(0,str(COMMON))
from scan_registry import RegistryError,boot_id,proc_identity
from screen_privacy import ScreenPrivacy
from test_stop_completion import controller_type

LAUNCHER=ROOT/'src/orangepi_collect/control/launcher.py'


def owner_exit_persistor():
    tree=ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Controller')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='_persist_owner_exit_proof')
    scope={'json':json,'time':time,'RegistryError':RegistryError,'atomic_json':lambda *_:None}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(LAUNCHER),'exec'),scope)
    return scope['_persist_owner_exit_proof']


def owner_finished_method():
    tree=ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Controller')
    method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='owner_finished')
    scope={'RegistryError':RegistryError}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(LAUNCHER),'exec'),scope)
    return scope['owner_finished']


class StopRecoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.privacy=ScreenPrivacy();self.privacy.committed=True
        self.privacy.phase='recording';self.privacy.nonce='stop-regression-nonce'
        self.privacy.session=str(self.path.resolve())
        self.privacy.committed_ns=time.monotonic_ns()-1_000_000
        self.privacy.start_proof={'worker_pid':123,'boot_id':boot_id(),
            'session_id':self.privacy.session,'nonce':self.privacy.nonce}
        self.gate={'stop_monotonic_ns':time.monotonic_ns(),'recording_active':False,
            'screen_privacy':self.privacy.start_proof.copy(),'worker_pid':123,'boot_id':boot_id()}
        self.complete={**self.gate,'native_reaped':True,'native_exit_code':0,
            'shutdown_complete':True,'capture_finalized':True,'summary_status':'success',
            'finalized_monotonic_ns':time.monotonic_ns()}
        self.c=controller_type()();self.c.path=self.path
        self.c.dashboard=SimpleNamespace(privacy=self.privacy)
        self.c.command=Mock();self.c.voice=SimpleNamespace(interrupt=Mock(),say=Mock())
        self.c.worker=Mock();self.c.worker.poll.return_value=None
        self.c.state='recording';self.c.state_write=Mock();self.c.last_help=[]
        self.c.capture_owner_released=False;self.c.owner_finished=Mock(return_value=False)

    def test_late_valid_capture_receipt_wins_after_worker_exit(self):
        self.c.stop_before_prompt()
        self.c.worker.poll.return_value=0
        self.c.capture_owner_exit_record={'run':'owner-exit-already-observed'}
        (self.path/'capture_complete.json').write_text(json.dumps(self.complete))
        persist=owner_exit_persistor()
        self.c.owner_finished=Mock(side_effect=lambda:persist(self.c))

        self.c.poll_stop()

        self.c.owner_finished.assert_called_once_with()
        self.assertFalse(self.privacy.committed)
        self.assertEqual(self.c.state,'confirm_stop')
        self.assertFalse(getattr(self.c,'owner_exit_recovered',False))

    def test_idle_recording_poll_without_stop_is_a_noop(self):
        self.c.stop_pending=False
        if hasattr(self.c,'stop_deadline'):del self.c.stop_deadline
        self.c.terminate_capture_owner=Mock(side_effect=AssertionError('idle recording poll must not signal'))

        self.c.poll_stop()

        self.c.command.assert_not_called()
        self.c.owner_finished.assert_not_called()
        self.c.terminate_capture_owner.assert_not_called()
        self.assertEqual(self.c.state,'recording')
        self.assertEqual(self.privacy.snapshot()['phase'],'recording')
        self.assertTrue(self.privacy.committed)
        self.assertFalse(hasattr(self.c,'stop_deadline'))

    def test_owner_inspect_ioerror_fails_closed_without_unbound_local(self):
        registry=SimpleNamespace(
            inspect=Mock(side_effect=OSError('injected registry read failure')),
            verify_exited=Mock(side_effect=RegistryError('no independent exit proof')))
        controller=SimpleNamespace(capture_owner_released=False,capture_owner_run='run-current',
            camera_registry=registry,dashboard=SimpleNamespace(privacy=self.privacy),
            state_write=Mock(),last_help=[])

        result=owner_finished_method()(controller)

        self.assertFalse(result)
        registry.inspect.assert_called_once_with()
        registry.verify_exited.assert_called_once_with('run-current','capture',None,None)
        self.assertTrue(self.privacy.committed)
        self.assertEqual(self.privacy.snapshot()['phase'],'recording')
        self.assertFalse(controller.capture_owner_released)
        self.assertTrue(any('隐私遮挡保持' in message for message in controller.last_help))
        controller.state_write.assert_called_once_with()

    def test_forty_five_second_warning_does_not_kill_live_owner(self):
        self.c.stop_before_prompt()
        hard_deadline=self.c.stop_force_deadline
        self.c.stop_deadline=time.monotonic()-1
        self.c.terminate_capture_owner=Mock(side_effect=AssertionError('45-second warning must not signal'))

        self.c.poll_stop()

        self.c.terminate_capture_owner.assert_not_called()
        self.assertTrue(self.privacy.committed)
        self.assertEqual(self.c.state,'recording')
        self.assertTrue(self.c.stop_retry_authorized)
        self.assertEqual(self.c.stop_escalation_phase,'waiting_user_retry')
        self.assertEqual(self.c.stop_force_deadline,hard_deadline)

    def test_verified_owner_exit_enters_error_ui(self):
        process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.15)'])
        try:
            identity=proc_identity(process.pid)
            self.assertIsNotNone(identity)
            process.wait(timeout=3)
            controller_identity=proc_identity(os.getpid())
            start={'schema_version':1,'policy':'screen_black_before_capture_v1',
                'nonce':self.privacy.nonce,'session_id':self.privacy.session,
                'controller_pid':os.getpid(),'controller_start_ticks':controller_identity['start'],
                'boot_id':boot_id(),'committed_monotonic_ns':self.privacy.committed_ns,
                'privacy_instance':self.privacy.instance,'privacy_generation':9,
                'owner_run':'stop-regression-run','worker_pid':process.pid,
                'worker_start_ticks':identity['start'],'adapter_pid':process.pid,
                'adapter_start_ticks':identity['start']}
            self.privacy.start_proof=start
            proof={'schema_version':1,'policy':'capture-owner-group-exit-v1',
                'session_id':self.privacy.session,'nonce':self.privacy.nonce,
                'privacy_instance':self.privacy.instance,'privacy_generation':9,
                'owner_run':start['owner_run'],'screen_privacy':start,
                'registry_run':start['owner_run'],'registry_nonce':'registry-nonce',
                'registry_kind':'capture','registry_finish_verified':False,
                'registry_liveness_verified':True,'registry_cleanup_confirmed':False,
                'group_absent':True,'adapter_reaped':True,
                'boot_id':boot_id(),'controller_pid':os.getpid(),
                'controller_start_ticks':controller_identity['start'],
                'adapter_pid':process.pid,'adapter_start_ticks':identity['start'],
                'worker_pid':process.pid,'worker_start_ticks':identity['start'],
                'pgid':process.pid,'sid':process.pid,'native_shutdown_confirmed':False,
                'native_qc_success':False,'observed_monotonic_ns':time.monotonic_ns()}
            self.c.worker.poll.return_value=process.returncode
            self.c.stop_pending=True;self.c.stop_deadline=time.monotonic()+45
            def owner_exited():
                self.assertTrue(self.privacy.confirm_owner_exited(proof))
                self.c.owner_exit_recovered=True
                self.c.owner_exit_proof=proof
                self.c.owner_exit_persist_error='injected diagnostic failure'
                return True
            self.c.owner_finished.side_effect=owner_exited

            self.c.poll_stop()

            self.assertFalse(self.privacy.committed)
            self.assertEqual(self.c.state,'error')
            self.assertTrue(any('标记失败' in message for message in self.c.last_help))
            self.assertTrue(any('恢复诊断未写入磁盘' in message for message in self.c.last_help))
            self.assertNotEqual(self.c.state,'confirm_stop')
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)


if __name__=='__main__':unittest.main()
