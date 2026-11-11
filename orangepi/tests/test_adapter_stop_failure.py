"""Stop/close failure regressions against the production adapter methods.

These tests extract only the side-effect methods under test from their source
AST and run them against temporary files and a fake reaped native process.
They never open hardware, send OS signals, or touch a real capture session.
"""
import ast
import json
import os
from pathlib import Path
import queue
import threading
import time
import unittest
from types import SimpleNamespace

SRC=Path(__file__).resolve().parents[1]/'src/orangepi_collect'


def adapter_methods():
    source=SRC/'framework/framework_recorder.py';tree=ast.parse(source.read_text(encoding='utf-8'))
    publish=next(node for node in tree.body if isinstance(node,ast.FunctionDef)
        and node.name=='publish_capture_completion')
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='PiBundleRecorder')
    record=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='_record')
    def atomic_json(path,value):Path(path).write_text(json.dumps(value))
    scope=dict(Path=Path,json=json,time=time,os=os,queue=queue,threading=threading,atomic_json=atomic_json)
    exec(compile(ast.Module(body=[publish,record],type_ignores=[]),str(source),'exec'),scope)
    return scope


class AdapterStopFailureTests(unittest.TestCase):
    def run_adapter(self,with_gate):
        scope=adapter_methods()
        with self.subTest(with_gate=with_gate),__import__('tempfile').TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'capture_summary.json').write_text(json.dumps(dict(
                status='success',errors=[],shutdown_complete=True,stop_monotonic_ns=1)))
            if with_gate:
                (root/'stop_gate.json').write_text(json.dumps(dict(
                    stop_monotonic_ns=1,worker_pid=123,recording_active=False)))
            owner=SimpleNamespace(config=SimpleNamespace(session_dir=temporary,
                stdin_control=False,wait_for_save=True),
                process=SimpleNamespace(pid=123,returncode=0,poll=lambda:0),
                stop_event=threading.Event(),commands=queue.Queue())
            errors=[]
            def invoke():
                try:scope['_record'](owner)
                except Exception as error:errors.append(str(error))
            thread=threading.Thread(target=invoke);thread.start();time.sleep(.12)
            status=json.loads((root/'status.json').read_text())
            result=dict(alive=thread.is_alive(),state=status['state'],
                complete=(root/'capture_complete.json').is_file(),errors=errors)
            if thread.is_alive():
                owner.stop_event.set();thread.join(1)
            result['exited_after_cancel']=not thread.is_alive()
            return result

    def test_missing_native_completion_gate_exits_instead_of_waiting_for_keep(self):
        result=self.run_adapter(with_gate=False)
        self.assertFalse(result['alive'])
        self.assertEqual(result['state'],'error')
        self.assertFalse(result['complete'])
        self.assertTrue(any('completion proof' in message for message in result['errors']))

    def test_valid_native_completion_gate_still_waits_for_explicit_save_decision(self):
        result=self.run_adapter(with_gate=True)
        self.assertTrue(result['alive'])
        self.assertEqual(result['state'],'awaiting_decision')
        self.assertTrue(result['complete'])
        self.assertTrue(result['exited_after_cancel'])

    def test_stop_command_is_queued_even_if_gate_write_raises(self):
        source=SRC/'acquisition/capture.py';tree=ast.parse(source.read_text(encoding='utf-8'))
        cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='Capture')
        method=next(node for node in cls.body if isinstance(node,ast.FunctionDef) and node.name=='request_stop')
        scope={};exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),scope)
        def broken_gate():raise OSError('injected stop-gate storage failure')
        owner=SimpleNamespace(stop_gate=broken_gate,error_list=[],commands=queue.Queue(),status=lambda *a,**k:None)
        scope['request_stop'](owner)
        self.assertEqual(owner.commands.get_nowait(),'STOP')
        self.assertTrue(owner.error_list)


if __name__=='__main__':unittest.main(verbosity=2)
