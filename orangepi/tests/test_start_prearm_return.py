"""A rejected START must not be reported as a successful dashboard action."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_stop_dashboard_dispatch import dashboard_tick_method

LAUNCHER = Path(__file__).resolve().parents[1] / 'src/orangepi_collect/control/launcher.py'


def key_method():
    tree = ast.parse(LAUNCHER.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'key')
    scope = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(LAUNCHER), 'exec'), scope)
    return scope['key']


class StartPrearmReturnTests(unittest.TestCase):
    def test_rejected_begin_reaches_dashboard_complete(self):
        dispatch = dashboard_tick_method()
        dispatch.__globals__['actions_for'] = lambda *_: ['start']
        privacy = SimpleNamespace(snapshot=Mock(return_value={'required': False}), target_matches=Mock())
        dashboard = SimpleNamespace(privacy=privacy, take=Mock(return_value={'action': 'start', 'revision': 2}), complete=Mock())
        owner = SimpleNamespace(state='ready', quit=False, dashboard=dashboard, ui_revision=2,
            worker=None, voice=SimpleNamespace(tick=Mock(), interrupt=Mock()), keyboard=None,
            selected_devices=[], ui_publish=Mock(), ensure_preview=Mock(), poll=Mock(),
            begin=Mock(return_value=False), reference_can_change=Mock(return_value=False),
            worker_status=Mock(return_value={}), OwnerExited=type('OwnerExited', (RuntimeError,), {}))
        owner.key = lambda key: key_method()(owner, key)
        dispatch(owner)
        owner.begin.assert_called_once_with()
        self.assertIn('未完成', dashboard.complete.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
