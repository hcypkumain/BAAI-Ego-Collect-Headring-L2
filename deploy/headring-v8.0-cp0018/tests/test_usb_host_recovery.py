import importlib.machinery,importlib.util,pathlib,unittest,contextlib
SRC=pathlib.Path(__file__).resolve().parents[1]/'system/headring-usb-data-host'
loader=importlib.machinery.SourceFileLoader('host_helper',str(SRC));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
class Guard:
 def __init__(self):self.idle=True
 @contextlib.contextmanager
 def lease(self):yield
 def allowed(self):return self.idle,'test_guard'
class Tests(unittest.TestCase):
 def setUp(self):
  self.now=0;self.guard=Guard();self.writes=[]
  self.s=dict(port_present=True,partner=True,key=(1,1),data_role='device',power_role='sink',driver_mode='device',host_children=[])
  self.host=m.DataHost(guard=self.guard,clock=lambda:self.now,writer=lambda p,v:self.writes.append((str(p),v)),snapshot=lambda:self.s.copy())
 def test_recovers_after_three_transient_failures_without_replug(self):
  for t in [0,11,42,103]:self.now=t;self.host.ensure()
  self.assertEqual(len(self.writes),4)
 def test_retry_backoff_prevents_role_flapping(self):
  for t in [0,1,2,9]:self.now=t;self.host.ensure()
  self.assertEqual(len(self.writes),1)
 def test_capture_guard_blocks_every_attempt(self):
  self.guard.idle=False
  for t in [0,11,42,103]:self.now=t;self.host.ensure()
  self.assertEqual(self.writes,[])
 def test_existing_children_are_never_reset(self):
  self.s['host_children']=['7-1'];self.host.ensure();self.assertEqual(self.writes,[])
 def test_host_role_is_idempotent_and_power_role_never_written(self):
  self.s.update(driver_mode='host',data_role='host');self.host.ensure();self.assertEqual(self.writes,[])
  self.s.update(driver_mode='device',data_role='device');self.host.ensure()
  self.assertTrue(all(path.endswith('/data_role') and value=='host' for path,value in self.writes))
if __name__=='__main__':unittest.main(verbosity=2)
