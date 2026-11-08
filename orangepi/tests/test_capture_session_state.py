"""Completed captures unblock background work; live/unreaped owners still block."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'src/orangepi_collect/common/capture_session_state.py'
spec=importlib.util.spec_from_file_location('session_state',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Tests(unittest.TestCase):
    def test_idle_completed_reference_is_not_active_session(self):
        path=Path('/saved/segment');owner=SimpleNamespace(path=path,worker=None,state='idle',last_review_path=path)
        self.assertIsNone(module.active_session(owner));self.assertEqual(owner.path,path);self.assertEqual(owner.last_review_path,path)
    def test_live_and_unreaped_worker_remains_active_even_if_idle(self):
        for state in ['idle','device_confirm','failed_wait']:
            owner=SimpleNamespace(path=Path('/take'),worker=object(),state=state)
            self.assertEqual(module.active_session(owner),str(Path('/take')))
    def test_capture_prepare_stop_save_states_remain_blocked(self):
        for state in ['preparing','ready','countdown','recording','confirm_stop','finalizing','error']:
            self.assertEqual(module.active_session(SimpleNamespace(path=Path('/take'),worker=None,state=state)),str(Path('/take')))
    def test_no_path_and_terminal_failure_do_not_invent_ownership(self):
        self.assertIsNone(module.active_session(SimpleNamespace(path=None,worker=object(),state='recording')))
        self.assertIsNone(module.active_session(SimpleNamespace(path=Path('/failed'),worker=None,state='failed_wait')))
    def test_both_controller_file_and_http_publish_use_same_helper(self):
        for name in ['launcher.py','web_controls.py']:
            text=(ROOT/'src/orangepi_collect/control'/name).read_text()
            self.assertIn('session=active_session(self)',text)
            self.assertNotIn('session=str(self.path) if self.path else None',text)
    def test_both_installers_initialize_result_parent(self):
        repo=ROOT.parent
        for name in ['scripts/install_pi_services.py','tools/provision/device_setup.py']:
            self.assertIn('/home/orangepi/maintenance/server-qc/results',(repo/name).read_text())

if __name__=='__main__':unittest.main(verbosity=2)
