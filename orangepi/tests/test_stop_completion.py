"""STOP cannot reveal a screen until this capture's native writer has finished."""
import ast
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/orangepi_collect/common'))
from screen_privacy import ScreenPrivacy


def controller_type():
    tree=ast.parse((ROOT/'src/orangepi_collect/control/launcher.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
    cls.bases=[]
    cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('stop_before_prompt','poll_stop')]
    scope={'time':time,'json':json}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),'launcher.py','exec'),scope)
    return scope['Controller']

def publisher():
    source=ROOT/'src/orangepi_collect/framework/framework_recorder.py'
    tree=ast.parse(source.read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='publish_capture_completion')
    def atomic(path,value):path.write_text(json.dumps(value))
    scope={'json':json,'time':time,'atomic_json':atomic}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),scope)
    return scope['publish_capture_completion']

class StopCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.privacy=ScreenPrivacy();self.privacy.committed=True
        self.privacy.phase='recording';self.privacy.nonce='n';self.privacy.session=str(self.path)
        self.privacy.committed_ns=time.monotonic_ns()-1000000
        self.privacy.start_proof={'worker_pid':123,'boot_id':'boot','session_id':str(self.path),'nonce':'n'}
        self.gate={'stop_monotonic_ns':time.monotonic_ns(),'recording_active':False,'screen_privacy':self.privacy.start_proof.copy(),'worker_pid':123,'boot_id':'boot'}
        self.complete={**self.gate,'native_reaped':True,'native_exit_code':0,'shutdown_complete':True,'capture_finalized':True,'summary_status':'success','finalized_monotonic_ns':time.monotonic_ns()}
        self.c=controller_type()();self.c.path=self.path;self.c.dashboard=SimpleNamespace(privacy=self.privacy)
        self.c.command=Mock();self.c.voice=SimpleNamespace(interrupt=Mock(),say=Mock());self.c.worker=Mock();self.c.state='recording';self.c.state_write=Mock()
        # These tests cover native completion receipts, not Registry exit recovery.
        self.c.worker.poll=Mock(return_value=None);self.c.capture_owner_released=False
        self.c.owner_finished=Mock(return_value=False)
    def test_gate_is_not_completion_and_duplicate_stop_is_idempotent(self):
        (self.path/'stop_gate.json').write_text(json.dumps(self.gate))
        self.assertTrue(self.c.stop_before_prompt());self.assertTrue(self.c.stop_before_prompt())
        self.c.command.assert_called_once_with('STOP');self.c.poll_stop()
        self.assertEqual(self.c.state,'recording');self.assertTrue(self.privacy.committed)
        (self.path/'capture_complete.json').write_text(json.dumps(self.complete));self.c.poll_stop()
        self.assertEqual(self.c.state,'confirm_stop');self.assertFalse(self.privacy.committed)
    def test_unfinished_or_foreign_receipt_never_unlocks(self):
        self.c.stop_before_prompt()
        for override in ({'native_reaped':False},{'native_exit_code':1},{'shutdown_complete':False},{'capture_finalized':False},
                         {'summary_status':'error'},{'worker_pid':124},
                         {'screen_privacy':{**self.gate['screen_privacy'],'nonce':'old'}}):
            with self.subTest(override=override):
                (self.path/'capture_complete.json').write_text(json.dumps({**self.complete,**override}))
                self.c.poll_stop();self.assertTrue(self.privacy.committed);self.assertEqual(self.c.state,'recording')
    def test_timeout_is_fault_not_success_and_can_retry(self):
        self.c.stop_before_prompt();self.c.stop_deadline=time.monotonic()-1;self.c.poll_stop()
        self.assertEqual(self.privacy.snapshot()['phase'],'fault');self.assertTrue(self.privacy.committed)
        self.assertEqual(self.c.state,'recording');self.assertTrue(self.c.stop_before_prompt())
        self.assertEqual(self.c.command.call_count,2)
        (self.path/'capture_complete.json').write_text(json.dumps(self.complete));self.c.poll_stop()
        self.assertEqual(self.c.state,'confirm_stop');self.assertFalse(self.privacy.committed)
    def test_adapter_publishes_only_after_matching_native_success(self):
        make=publisher();proc=SimpleNamespace(pid=123,poll=Mock(return_value=None))
        summary={'status':'success','shutdown_complete':True,'stop_monotonic_ns':self.gate['stop_monotonic_ns']}
        (self.path/'stop_gate.json').write_text(json.dumps(self.gate))
        self.assertFalse(make(self.path,proc,summary))
        proc.poll.return_value=1
        self.assertFalse(make(self.path,proc,summary))
        proc.poll.return_value=0
        self.assertFalse(make(self.path,proc,{**summary,'status':'error'}))
        proc.pid=124
        self.assertFalse(make(self.path,proc,summary))
        self.assertFalse((self.path/'capture_complete.json').exists())
        proc.pid=123
        self.assertTrue(make(self.path,proc,summary))
        self.assertTrue(self.privacy.confirm_stopped(json.loads((self.path/'capture_complete.json').read_text())))
    def test_capture_error_after_proven_shutdown_is_not_success(self):
        make=publisher();proc=SimpleNamespace(pid=123,poll=Mock(return_value=1))
        (self.path/'stop_gate.json').write_text(json.dumps(self.gate))
        summary={'status':'error','shutdown_complete':True,'stop_monotonic_ns':self.gate['stop_monotonic_ns']}
        self.assertFalse(make(self.path,proc,{**summary,'shutdown_complete':False}))
        self.assertTrue(make(self.path,proc,summary));self.c.stop_before_prompt();self.c.poll_stop()
        self.assertFalse(self.privacy.committed);self.assertEqual(self.c.state,'error')
        self.assertIn('未标记为成功',self.c.last_help[0])

if __name__=='__main__':unittest.main()
