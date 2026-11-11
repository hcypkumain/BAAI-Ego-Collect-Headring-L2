import ast,json,pathlib,subprocess,types,unittest,os,threading,uuid,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/common'))
from scan_result import encode,decode
from required_device_contract import build_contract
from required_capture_fixture import config as device_config
from unittest.mock import Mock
ROOT=pathlib.Path(os.environ.get('HOTPLUG_SOURCE_ROOT',str(pathlib.Path(__file__).resolve().parents[1]/'src/orangepi_collect/control')))

def controller_class():
 p=ROOT/'launcher.py';tree=ast.parse(p.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
 cls.bases=[];cls.body=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('scan_devices','_scan_devices','poll_hotplug')]
 clock=types.SimpleNamespace(value=100.);clock.monotonic=lambda:clock.value
 settings=device_config('emg');settings['auto_discovery']={'enabled':True,'poll_seconds':2,'retry_no_data_seconds':15}
 required=build_contract(settings)['required_devices']
 wrist_fingerprint=(('wrist','/dev/wrist-left','/dev/video1','serial-left',1),
                    ('wrist','/dev/wrist-right','/dev/video2','serial-right',1))
 runner=types.SimpleNamespace(run=Mock(),TimeoutExpired=subprocess.TimeoutExpired)
 def run_scan(*a,**kw):
  result=runner.run(*a,**kw)
  if result.returncode:raise ValueError('scan failed')
  request=json.loads(kw['env']['ORANGEPI_SCAN_REQUEST'])
  try:rows=json.loads(result.stdout);value=encode('run','nonce',request['id'],request['fingerprint'],rows)
  except ValueError:value=result.stdout
  return value,'run','nonce'
 scope=dict(uuid=uuid,os=os,decode_scan_result=decode,time=clock,devices=lambda:settings,fingerprint=lambda *_:wrist_fingerprint,json=json,subprocess=runner,run_scan=run_scan,ScanProcessError=type('ScanProcessError',(Exception,),{}),BASE=pathlib.Path('/unused'),SRC=pathlib.Path('/unused/src'),atomic_json=Mock())
 exec(compile(ast.Module(body=[cls],type_ignores=[]),str(p),'exec'),scope)
 c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.camera_registry=object();c.scan_lock=threading.Lock();c.state='starting';c.worker=None;c.keyboard=None;c.last_scan=[];c.last_usb_poll=0;c.last_device_scan=0
 c.stop_preview=Mock();c.state_write=Mock();c.voice=Mock()
 return c,clock,runner,required

class RecoveryTests(unittest.TestCase):
 def ready_rows(self,required):
  rows=[]
  for name in required:
   row=dict(name=name,label=name,connected=True,present=True,status='ready')
   if name.startswith('cam_ros_'):row.update(fresh_frame=True,probe_scope='camd_snapshot')
   elif name=='cam_left_wrist':row['device']='/dev/wrist-left'
   elif name=='cam_right_wrist':row['device']='/dev/wrist-right'
   rows.append(row)
  return rows

 def test_initial_failed_scan_retries_unchanged_usb_after_deadline(self):
  for failure in [subprocess.TimeoutExpired('scan',15),'invalid_json','nonzero_exit']:
   with self.subTest(failure=str(failure)):
    c,clock,runner,_=controller_class()
    if isinstance(failure,Exception):runner.run.side_effect=failure
    else:runner.run.return_value=types.SimpleNamespace(returncode=1 if failure=='nonzero_exit' else 0,stdout='not json')
    self.assertFalse(c.scan_devices(announce=False))
    c.scan_devices=Mock();clock.value=105;c.poll_hotplug();c.scan_devices.assert_not_called()
    clock.value=116;c.poll_hotplug();c.scan_devices.assert_called_once_with(announce=False)
 def test_successful_retry_stops_retrying_without_device_changes(self):
  c,clock,runner,required=controller_class();c.scan_retry_pending=True
  runner.run.return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(self.ready_rows(required)))
  self.assertTrue(c.scan_devices(announce=False));self.assertTrue(c.scan_ok);self.assertEqual(c.required_devices_missing,[])
  c.scan_devices=Mock();clock.value=116;c.poll_hotplug();c.scan_devices.assert_not_called()
 def test_valid_empty_inventory_does_not_create_retry_loop_or_pass_required_gate(self):
  c,clock,runner,required=controller_class();runner.run.return_value=types.SimpleNamespace(returncode=0,stdout='[]')
  self.assertTrue(c.scan_devices(announce=False));self.assertFalse(c.scan_ok)
  self.assertEqual(set(c.required_devices_missing),set(required))
  c.scan_devices=Mock();clock.value=116;c.poll_hotplug();c.scan_devices.assert_not_called()
 def test_failed_scan_never_overrides_recording_ownership(self):
  c,clock,runner,_=controller_class();c.scan_retry_pending=True;c.scan_devices=Mock();c.worker=Mock();clock.value=116
  for state in ['preparing','recording','countdown','finalizing','confirm_stop']:
   c.state=state;c.poll_hotplug()
  c.scan_devices.assert_not_called()
 def test_present_but_silent_device_keeps_existing_retry(self):
  c,clock,runner,_=controller_class();c.state='device_confirm';c.last_scan=[dict(present=True,connected=False)];c.usb_fingerprint=(('wrist','/dev/current','/dev/video1','serial',1),);c.scan_devices=Mock()
  clock.value=116;c.poll_hotplug();c.scan_devices.assert_called_once_with(announce=False)

class StartupTests(unittest.TestCase):
 def test_initial_dashboard_snapshot_has_renderable_starting_state(self):
  tree=ast.parse((ROOT/'dashboard.py').read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Dashboard')
  init=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__init__')
  assignment=next(n for n in ast.walk(init) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Attribute) and t.attr=='snapshot' for t in n.targets))
  value=ast.literal_eval(assignment.value)
  self.assertEqual(value.get('state'),'starting');self.assertEqual(value.get('devices'),[]);self.assertIsNone(value.get('session'))

if __name__=='__main__':unittest.main(verbosity=2)
