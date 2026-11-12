import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class ControllerLoopDiagnostics(unittest.TestCase):
    def test_voice_prompt_gap_does_not_emit_loop_lag_but_tick_gap_does(self):
        source=Path(__file__).resolve().parents[1]/'src/orangepi_collect/control/web_controls.py'
        tree=ast.parse(source.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='WebControls')
        methods=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('voice_prompt_changed','dashboard_tick')]
        clock=SimpleNamespace(value=0,monotonic=lambda:clock.value)
        scope={'time':clock};exec(compile(ast.Module(body=methods,type_ignores=[]),str(source),'exec'),scope)
        dashboard=SimpleNamespace(events=SimpleNamespace(emit=Mock()),set_prompting=Mock(),take=Mock(return_value=None))
        owner=SimpleNamespace(dashboard=dashboard,voice=SimpleNamespace(tick=Mock()),ui_publish=Mock(),ensure_preview=Mock())
        scope['voice_prompt_changed'](owner,True);clock.value=20;scope['voice_prompt_changed'](owner,False)
        dashboard.events.emit.assert_not_called()
        scope['dashboard_tick'](owner);clock.value=22;scope['dashboard_tick'](owner)
        dashboard.events.emit.assert_called_once_with('loop_lag',lag_ms=2000)


if __name__=='__main__':unittest.main()
