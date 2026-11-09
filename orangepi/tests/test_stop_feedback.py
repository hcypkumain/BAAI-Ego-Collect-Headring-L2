"""Exercise production result publication without loading hardware drivers."""
import ast
import copy
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control/dashboard.py'


def methods():
    tree = ast.parse(SOURCE.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(
        isinstance(m, ast.FunctionDef) and m.name == 'display_state' for m in n.body))
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name in ('take', 'complete', 'display_state')]
    cls.bases = []
    scope = dict(copy=copy, time=time)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope[cls.name]


class StopFeedbackTests(unittest.TestCase):
    def test_failed_queued_stop_publishes_its_action_and_error_on_lightweight_gate(self):
        d = methods()()
        d.lock = threading.RLock()
        d.pending = {'action': 'stop', 'revision': 7}
        d.busy = False
        d.snapshot = {'state': 'recording', 'worker': {'recording_active': True}}
        d.privacy = SimpleNamespace(snapshot=lambda: {'required': True})
        d.result = None
        self.assertEqual(d.take()['action'], 'stop')
        d.complete('流程已经变化，本次点击未执行')
        gate = d.display_state()
        self.assertFalse(gate['busy'])
        self.assertEqual(gate['result']['action'], 'stop')
        self.assertFalse(gate['result']['ok'])
        self.assertTrue(gate['worker']['recording_active'])
        gate['result']['error'] = 'client mutation'
        self.assertNotEqual(d.result['error'], 'client mutation')

    def test_other_action_result_is_identified_separately(self):
        d = methods()();d.lock = threading.RLock();d.pending = {'action': 'start'}
        d.take();d.complete()
        self.assertEqual(d.result['action'], 'start')
        self.assertTrue(d.result['ok'])


if __name__ == '__main__':
    unittest.main()
