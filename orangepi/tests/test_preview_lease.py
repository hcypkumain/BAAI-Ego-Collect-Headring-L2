"""Only manual EMG confirmation reserves the capture lease during preview."""
import ast
from contextlib import contextmanager
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1]/'src/orangepi_collect/acquisition/preview_worker.py'


def preview_entry(main, guard, output):
    # Compile the actual entry function without importing ROS or device drivers.
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'run_preview')
    space = {'main': main, 'print': output,
             'wrist_camera_settings': types.SimpleNamespace(capture_guard=guard),
             'project_paths': types.SimpleNamespace(BASE=Path('/test/pi'))}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), space)
    return space['run_preview']


class PreviewLeaseTests(unittest.TestCase):
    def test_manual_main_runs_while_capture_lease_is_held(self):
        state = {'held': False}; events = []; config = {'_exclusive_preview': True}
        @contextmanager
        def lease(base):
            self.assertEqual(base, Path('/test/pi'))
            state['held'] = True; events.append('acquire')
            try: yield
            finally: state['held'] = False; events.append('release')
        def main(value):
            self.assertIs(value, config)
            self.assertTrue(state['held']); events.append('main')
        guard = Mock(side_effect=lease); callback = Mock(side_effect=main)
        preview_entry(callback, guard, Mock())(config)
        callback.assert_called_once_with(config); guard.assert_called_once_with(Path('/test/pi'))
        self.assertEqual(events, ['acquire', 'main', 'release'])
        self.assertFalse(state['held'])

    def test_ordinary_preview_does_not_acquire_or_test_the_capture_lease(self):
        for config in ({}, {'_exclusive_preview': False}, {'_exclusive_preview': None}):
            with self.subTest(config=config):
                guard = Mock(side_effect=AssertionError('Normal preview must leave export lease available'))
                main = Mock()
                preview_entry(main, guard, Mock())(config)
                main.assert_called_once_with(config); guard.assert_not_called()

    def test_manual_lock_conflict_never_starts_device_preview(self):
        @contextmanager
        def busy_lease(base):
            raise ValueError('capture lease already held')
            yield
        main = Mock(); output = Mock(); guard = Mock(side_effect=busy_lease)
        preview_entry(main, guard, output)({'_exclusive_preview': True})
        guard.assert_called_once_with(Path('/test/pi')); main.assert_not_called()
        output.assert_called_once_with('PREVIEW unavailable: capture lease already held', flush=True)

    def test_manual_preview_failure_releases_lease_before_propagating(self):
        state = {'held': False}
        @contextmanager
        def lease(base):
            state['held'] = True
            try: yield
            finally: state['held'] = False
        def fail(config):
            self.assertTrue(state['held'])
            raise RuntimeError('preview test failure')
        main = Mock(side_effect=fail); guard = Mock(side_effect=lease)
        with self.assertRaisesRegex(RuntimeError, 'preview test failure'):
            preview_entry(main, guard, Mock())({'_exclusive_preview': True})
        self.assertFalse(state['held'])
        main.assert_called_once(); guard.assert_called_once()


if __name__ == '__main__':
    unittest.main()
