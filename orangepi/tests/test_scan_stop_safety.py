import ast
import fcntl
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(ROOT/'common'),str(ROOT/'control')]
from scan_registry import Registry, RegistryError
from scan_process import run_scan, ScanProcessError
from owner_test_support import OwnerTestSupport


def controller_type():
    path=ROOT/'control/launcher.py';tree=ast.parse(path.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
    cls.bases=[];cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('scan_prohibited','scan_cancel_requested','scan_devices','poll_hotplug')]
    scope={'time':time,'fingerprint':lambda _:('changed',),'devices':lambda:{'auto_discovery':{}}}
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
    return scope['Controller']


class ScanStopSafety(unittest.TestCase):
    def test_owner_and_unknown_privacy_block_before_cleanup_or_probe(self):
        c=controller_type()();c.scan_lock=threading.Lock();c.retry_pending_owner=Mock();c._scan_devices=Mock();c.state='ready';c.worker=object()
        self.assertFalse(c.scan_devices());c.retry_pending_owner.assert_not_called();c._scan_devices.assert_not_called()
        c.worker=None;c.state='idle';c.dashboard=SimpleNamespace(lock=threading.Lock(),pending=None,busy=False,privacy=SimpleNamespace(snapshot=lambda:{'required':True,'stop_confirmed':False}),events=None)
        self.assertFalse(c.scan_devices());c.retry_pending_owner.assert_not_called();c._scan_devices.assert_not_called()
        c.dashboard.privacy=SimpleNamespace(snapshot=lambda:{'required':False,'stop_confirmed':True})
        c.dashboard.pending={'action':'start'}
        self.assertFalse(c.scan_devices());c.retry_pending_owner.assert_not_called();c._scan_devices.assert_not_called()
    def test_ready_hotplug_defers_without_releasing_owner(self):
        c=controller_type()();c.state='ready';c.worker=object();c.last_usb_poll=0;c.usb_fingerprint=('old',)
        c.release_prepared=Mock();c.scan_devices=Mock();c.poll_hotplug()
        self.assertTrue(c.scan_retry_pending);c.release_prepared.assert_not_called();c.scan_devices.assert_not_called()
    def test_dispatch_cannot_release_owner_for_rescan_or_settings(self):
        from test_stop_dashboard_dispatch import dashboard_tick_method
        for action in ('rescan','calibrate_wrist','calibrate_emg','eeg_filter','wrist_camera_settings'):
            dispatch=dashboard_tick_method();dispatch.__globals__['actions_for']=lambda *_:[action];dispatch.__globals__['time']=time
            job={'action':action,'revision':1,'value':None}
            privacy=SimpleNamespace(snapshot=Mock(return_value={'required':False,'stop_confirmed':True}),target_matches=Mock(return_value=False))
            dashboard=SimpleNamespace(privacy=privacy,take=Mock(return_value=job),complete=Mock())
            owner=SimpleNamespace(state='ready',dashboard=dashboard,ui_revision=1,selected_devices=[],
                voice=SimpleNamespace(tick=Mock(),interrupt=Mock()),ui_publish=Mock(),keyboard=None,
                scan_prohibited=Mock(return_value=True),release_prepared=Mock(),ensure_preview=Mock(),
                reference_can_change=Mock(return_value=False),wrist_settings_available=Mock(return_value=True),
                OwnerExited=type('OwnerExited',(RuntimeError,),{}))
            dispatch(owner)
            owner.release_prepared.assert_not_called()
            self.assertIn('先退出',dashboard.complete.call_args.args[0])
    def test_flock_timeout_does_not_steal_or_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=Registry(tmp,lock_timeout=.08);lock=Path(tmp)/'camera_owner.lock'
            with lock.open('w') as f:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                started=time.monotonic()
                with self.assertRaisesRegex(RegistryError,'所有权未知'):
                    with registry.locked():self.fail('must not enter')
                self.assertLess(time.monotonic()-started,.5)
                self.assertFalse((Path(tmp)/'camera_owner.json').exists())
    def test_inspect_failure_preserves_unknown_child_for_cleanup(self):
        child=SimpleNamespace(pid=12345);registry=SimpleNamespace(inspect=Mock(side_effect=RegistryError('busy')))
        with patch('scan_process.spawn_owned',return_value=(child,'run')):
            with self.assertRaises(ScanProcessError) as raised:run_scan(['unused'],registry)
        self.assertTrue(raised.exception.cleanup_pending)
        self.assertIs(raised.exception.child,child)
        self.assertEqual(raised.exception.run,'run')
    def test_cancellable_owned_wait_returns_before_probe_timeout(self):
        support=OwnerTestSupport().start();self.addCleanup(support.stop)
        with tempfile.TemporaryDirectory() as tmp:
            c=controller_type()();c.state='scanning';c.worker=None;c.quit=False
            c.dashboard=SimpleNamespace(lock=threading.Lock(),pending=None,pending_stop=None,busy=False,
                privacy=SimpleNamespace(snapshot=lambda:{'required':False,'stop_confirmed':True}))
            def queue_stop():
                with c.dashboard.lock:c.dashboard.pending_stop={'action':'stop'}
            timer=threading.Timer(.25,queue_stop);timer.start();self.addCleanup(timer.cancel)
            started=time.monotonic()
            with self.assertRaises(ScanProcessError):
                run_scan([sys.executable,'-c','import time;time.sleep(30)'],Registry(tmp),timeout=15,
                         cancel=c.scan_cancel_requested)
            self.assertLess(time.monotonic()-started,3)
            self.assertEqual(c.dashboard.pending_stop,{'action':'stop'},'priority STOP remains queued for dispatcher')


if __name__=='__main__':unittest.main()
