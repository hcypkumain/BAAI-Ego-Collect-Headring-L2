"""Real crash windows; camera resources represented only by regular-file FDs."""
import os,pathlib,signal,subprocess,sys,tempfile,time,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'
sys.path.insert(0,str(ROOT))
import scan_registry as reg
from owner_test_support import OwnerTestSupport

DRIVER=r'''
import os,signal,subprocess,sys,time,pathlib
sys.path.insert(0,sys.argv[1]);import scan_registry as module;module.validate_owned_command=lambda *args:None;module.WRAPPER_PATH=pathlib.Path(sys.argv[4]);Registry=module.Registry;spawn_owned=module.spawn_owned
root=pathlib.Path(sys.argv[2]);stage=sys.argv[3];r=Registry(root);ready=root/'ready';target=root/'held';target.write_text('ordinary file')
if stage in ('reserved','spawning'):
 run=r.reserve('scan')
 if stage=='spawning':
  def stop_before_exec():
   (root/'pid').write_text(str(os.getpid()));os.kill(os.getpid(),signal.SIGSTOP)
  subprocess.Popen([sys.executable,'-c','pass'],preexec_fn=stop_before_exec)
 else:ready.write_text('reserved')
 while True:time.sleep(10)
if stage=='registered':
 code="import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text('registered-before-probe');time.sleep(60)"
 p,run=spawn_owned(r,'scan',[sys.executable,'-c',code,str(root/'registered')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 (root/'pid').write_text(str(p.pid))
 while not (root/'registered').exists():time.sleep(.01)
 ready.write_text(stage)
 while True:time.sleep(10)
code="import os,signal,subprocess,sys,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);f=open(sys.argv[1]);p=subprocess.Popen([sys.executable,'-c','import signal,time,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);f=open(sys.argv[1]);time.sleep(60)',sys.argv[1]]);pathlib.Path(sys.argv[2]).write_text(str(p.pid));time.sleep(60)"
p,run=spawn_owned(r,'scan',[sys.executable,'-c',code,str(target),str(root/'grandchild')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
(root/'pid').write_text(str(p.pid))
while not (root/'grandchild').exists():time.sleep(.01)
if stage=='timeout':os.killpg(p.pid,signal.SIGTERM)
if stage=='cleaned':
 os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=2)
ready.write_text(stage)
while True:time.sleep(10)
'''
class CrashWindows(unittest.TestCase):
 def setUp(self):
  self.harness=OwnerTestSupport().start();self.addCleanup(self.harness.stop)
 def test_crash_windows_remain_blocked_or_reconcile_only_after_group_exit(self):
  for stage in ('reserved','spawning','registered','probe','timeout','cleaned'):
   with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
    root=pathlib.Path(directory);controller=subprocess.Popen([sys.executable,'-c',DRIVER,str(ROOT),directory,stage,str(self.harness.path/'camera_owner_wrapper.py')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    target=root/('pid' if stage=='spawning' else 'ready');deadline=time.monotonic()+5
    try:
     while not target.exists() and controller.poll() is None and time.monotonic()<deadline:time.sleep(.01)
     self.assertTrue(target.exists(),'phase '+stage+' did not complete')
     controller.kill();controller.wait(timeout=2);registry=reg.Registry(root);value=registry.inspect();self.assertIsNotNone(value)
     if stage!='cleaned':
      for kind in ('scan','preview','capture'):
       with self.assertRaises(reg.RegistryError):registry.reserve(kind)
     if stage=='cleaned':
      try:registry.reconcile()
      except reg.RegistryError:
       # A retained zombie still means no proof the group is absent.
       self.assertIsNotNone(registry.inspect())
      else:self.assertIsNone(registry.inspect())
    finally:
     if controller.poll() is None:controller.kill();controller.wait(timeout=2)
     pidpath=root/'pid'
     if pidpath.exists():
      pid=int(pidpath.read_text())
      try:
       if stage=='spawning':os.kill(pid,signal.SIGKILL)
       elif stage!='cleaned':os.killpg(pid,signal.SIGKILL)
      except ProcessLookupError:pass
