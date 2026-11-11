"""Owner-bound STOP recovery with real disposable process groups."""
import ast
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
COMMON=ROOT/'src/orangepi_collect/common'
LAUNCHER=ROOT/'src/orangepi_collect/control/launcher.py'
sys.path.insert(0,str(COMMON))
from scan_registry import Registry,RegistryError,boot_id,group_exists,proc_identity
from screen_privacy import ScreenPrivacy


def terminate_controller(atomic_json_impl=None):
    tree=ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Controller')
    names={'terminate_capture_owner','_persist_owner_exit_proof','owner_finished','poll_stop'}
    methods=[node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name in names]
    cls.bases=[];cls.body=methods
    def atomic_json(path,value):Path(path).write_text(__import__('json').dumps(value))
    scope={'os':os,'signal':signal,'time':time,'json':__import__('json'),
           'RegistryError':RegistryError,'atomic_json':atomic_json_impl or atomic_json}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(LAUNCHER),'exec'),scope)
    return scope['Controller']


class StopOwnerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.registry=Registry(self.root/'runtime')

    def spawn_group(self):
        run=self.registry.reserve('capture')
        child_file=self.root/'child.pid'
        source=("import os,signal,sys,time\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "from scan_registry import Registry\n"
                "root,run,child_file=sys.argv[2:]\n"
                "child=os.fork()\n"
                "if child==0:\n"
                " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                " open(child_file,'w').write(str(os.getpid()))\n"
                " while True: time.sleep(.1)\n"
                "Registry(root).register(run)\n"
                "while True: time.sleep(.1)\n")
        process=subprocess.Popen([sys.executable,'-c',source,str(COMMON),str(self.root/'runtime'),run,str(child_file)],
                                 start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            record=self.registry.inspect()
            if record and record.get('run')==run and record.get('phase')=='registered' and child_file.exists():
                return run,process,record,int(child_file.read_text())
            time.sleep(.02)
        self.fail('registered dummy process group did not start')

    def controller(self,run,process,record,atomic_json_impl=None):
        session=self.root/'session';session.mkdir(exist_ok=True)
        privacy=ScreenPrivacy();privacy.committed=True;privacy.phase='fault';privacy.nonce='privacy-nonce'
        privacy.session=str(session.resolve());privacy.committed_ns=time.monotonic_ns()-1
        privacy.start_proof=dict(schema_version=1,policy='screen_black_before_capture_v1',
            nonce=privacy.nonce,session_id=privacy.session,controller_pid=os.getpid(),
            controller_start_ticks=proc_identity(os.getpid())['start'],boot_id=boot_id(),
            committed_monotonic_ns=privacy.committed_ns,privacy_instance=privacy.instance,
            privacy_generation=9,owner_run=run,worker_pid=process.pid,
            worker_start_ticks=record['wrapper_start'],adapter_pid=process.pid,
            adapter_start_ticks=record['wrapper_start'])
        owner=terminate_controller(atomic_json_impl)();owner.camera_registry=self.registry;owner.capture_owner_run=run
        owner.worker=process;owner.dashboard=SimpleNamespace(privacy=privacy)
        owner.path=session
        return owner,privacy

    def test_owner_exit_with_diagnostic_write_failure_shows_error_but_keeps_registry(self):
        run,leader,record,_=self.spawn_group()
        self.addCleanup(self._cleanup_group,run,leader)
        def fail_diagnostic(_path,_value):raise OSError('injected diagnostic disk failure')
        owner,privacy=self.controller(run,leader,record,fail_diagnostic)
        owner.state='recording';owner.last_help=[];owner.stop_pending=True
        owner.stop_deadline=time.monotonic()+45;owner.state_write=lambda:None
        owner.voice=SimpleNamespace(say=lambda _:None)
        os.killpg(record['pgid'],signal.SIGKILL);leader.wait(timeout=3)
        deadline=time.monotonic()+3
        while group_exists(record['pgid']) and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(group_exists(record['pgid']))

        finish=self.registry.finish
        self.registry.finish=lambda _run: (_ for _ in ()).throw(OSError('injected registry cleanup failure'))
        self.addCleanup(setattr,self.registry,'finish',finish)
        owner.poll_stop()

        self.assertEqual(owner.state,'error')
        self.assertFalse(privacy.committed)
        self.assertTrue(owner.owner_exit_recovered)
        self.assertTrue(owner.capture_registry_cleanup_pending)
        self.assertTrue(any('停止恢复诊断未写入磁盘' in item for item in owner.last_help))
        self.assertEqual(self.registry.inspect()['run'],run)
        with self.assertRaises(RegistryError):self.registry.reserve('capture')

    def test_term_exit_of_leader_then_verified_kill_of_residual_child(self):
        run,leader,record,child_pid=self.spawn_group()
        self.addCleanup(self._cleanup_group,run,leader)
        owner,privacy=self.controller(run,leader,record)
        self.assertTrue(owner.terminate_capture_owner(signal.SIGTERM))
        leader.wait(timeout=3)
        self.assertTrue(group_exists(record['pgid']), 'TERM-ignoring child should remain')
        self.assertTrue(owner.terminate_capture_owner(signal.SIGKILL))
        deadline=time.monotonic()+3
        while group_exists(record['pgid']) and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(group_exists(record['pgid']))
        exited=self.registry.verify_exited(run,'capture')
        self.assertTrue(exited['liveness_verified']);self.assertFalse(exited['registry_cleanup_confirmed'])
        self.registry.finish(run)
        proof=dict(schema_version=1,policy='capture-owner-group-exit-v1',
            session_id=privacy.session,nonce=privacy.nonce,privacy_instance=privacy.instance,
            privacy_generation=privacy.start_proof['privacy_generation'],owner_run=run,
            screen_privacy=privacy.start_proof,registry_run=run,registry_nonce=record['nonce'],
            registry_kind='capture',registry_finish_verified=True,registry_liveness_verified=True,
            registry_cleanup_confirmed=True,group_absent=True,adapter_reaped=True,
            boot_id=record['boot'],controller_pid=record['owner_pid'],controller_start_ticks=record['owner_start'],
            adapter_pid=record['wrapper_pid'],adapter_start_ticks=record['wrapper_start'],
            worker_pid=record['wrapper_pid'],worker_start_ticks=record['wrapper_start'],
            pgid=record['pgid'],sid=record['sid'],native_shutdown_confirmed=False,native_qc_success=False,
            observed_monotonic_ns=time.monotonic_ns())
        self.assertTrue(privacy.confirm_owner_exited(proof))
        self.assertFalse(privacy.snapshot()['required'])
        self.assertFalse(privacy.confirm_owner_exited(dict(proof,nonce='old-session')))
        # The verified group is gone after SIGKILL, so the former child PID
        # must no longer identify a live process.
        self.assertFalse(group_exists(record['pgid']))

    def test_other_session_or_live_group_cannot_release_or_be_signalled(self):
        run,leader,record,_=self.spawn_group()
        self.addCleanup(self._cleanup_group,run,leader)
        owner,privacy=self.controller(run,leader,record)
        # A stale session nonce is rejected by the privacy gate, even when the
        # registry belongs to this controller. A matching proof is also
        # rejected while the registered process group is still alive.
        proof=dict(schema_version=1,policy='capture-owner-group-exit-v1',session_id=privacy.session,
            nonce='stale-nonce',privacy_instance=privacy.instance,
            privacy_generation=privacy.start_proof['privacy_generation'],owner_run=run,
            screen_privacy=privacy.start_proof,registry_run=run,registry_kind='capture',
            registry_finish_verified=False,registry_liveness_verified=True,registry_cleanup_confirmed=False,
            group_absent=True,adapter_reaped=True,boot_id=record['boot'],controller_pid=record['owner_pid'],
            controller_start_ticks=record['owner_start'],adapter_pid=record['wrapper_pid'],
            adapter_start_ticks=record['wrapper_start'],worker_pid=record['wrapper_pid'],
            worker_start_ticks=record['wrapper_start'],pgid=record['pgid'],sid=record['sid'],
            native_shutdown_confirmed=False,native_qc_success=False,observed_monotonic_ns=time.monotonic_ns())
        self.assertFalse(privacy.confirm_owner_exited(proof))
        self.assertFalse(privacy.confirm_owner_exited(dict(proof,nonce=privacy.nonce)))
        self.assertTrue(group_exists(record['pgid']))

    def _cleanup_group(self,run,process):
        try:
            record=self.registry.inspect()
            if record and record.get('run')==run:
                try:os.killpg(record['pgid'],signal.SIGKILL)
                except ProcessLookupError:pass
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:pass
                deadline=time.monotonic()+2
                while group_exists(record['pgid']) and time.monotonic()<deadline:time.sleep(.02)
                if not group_exists(record['pgid']):self.registry.finish(run)
        except (OSError,RegistryError):pass


if __name__=='__main__':unittest.main()
