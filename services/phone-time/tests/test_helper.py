import hashlib,importlib.machinery,importlib.util,sys,types,unittest,io,json,contextlib,tempfile,fcntl,os,time
from pathlib import Path
from unittest.mock import patch
p=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(p))
loader=importlib.machinery.SourceFileLoader('phone_time',str(p/'headring-phone-time'));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
class Guard:
 allow=True
 base=None
 @contextlib.contextmanager
 def lease(self):yield
 def allowed(self):return self.allow,'camera_idle' if self.allow else 'busy'
class Input:
 def __init__(self,data):self.buffer=io.BytesIO(data)
class T(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.lock=Path(self.tmp.name)/'lock';self.identity=Path(self.tmp.name)/'DEVICE_ID';self.identity.write_text('cp_0023'); Guard.base=Path(self.tmp.name); (Guard.base/'runtime').mkdir(); (Guard.base/'runtime/dashboard_token').write_text('secret'); (Guard.base/'pair-hash').write_text(hashlib.sha256(('a'*32).encode()).hexdigest()); (Guard.base/'runtime/collection_profile.json').write_text(json.dumps({'legacy_mode':'eeg','active_mode':'eeg'}))
 def tearDown(self):self.tmp.cleanup()
 def run_request(self,data,allow=True):
  try:
   value=json.loads(data)
   if isinstance(value,dict) and 'received_monotonic_ns' not in value:
    value['received_monotonic_ns']=time.monotonic_ns();data=json.dumps(value).encode()
  except (ValueError,TypeError):pass
  Guard.allow=allow;os_open=m.os.open
  def opening(path,flags,mode=0o777):return os_open(str(self.lock) if path=='/run/headring-lan-time.lock' else path,flags,mode)
  original=m.Path
  def mapped(path):return self.identity if path=='/opt/egorecord/DEVICE_ID' else original(self.tmp.name)/'pair-hash' if path=='/etc/headring-phone-time.sha256' else original(self.tmp.name)/'marker.json' if path=='/run/headring-phone-time.json' else original(path)
  with patch.object(m,'CaptureGuard',Guard),patch.object(m.os,'open',side_effect=opening),patch.object(m,'Path',side_effect=mapped),patch.object(m.sys,'stdin',Input(data)),patch.object(m.time,'clock_settime') as setter,patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)),patch.object(m.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(b'{"collection_context":"eeg:1","state":"device_confirm","privacy":{"required":false,"phase":"idle","stop_confirmed":true}}')),contextlib.redirect_stdout(io.StringIO()):
   try:m.main();return True,setter.call_count
   except (ValueError,TypeError,json.JSONDecodeError):return False,setter.call_count
 def test_accept_and_reject(self):
  good={'device_id':'cp_0023','epoch_ms':1794900000000,'collection_context':'eeg:1'};self.assertEqual(self.run_request(json.dumps(good).encode()),(True,1));self.assertEqual(self.run_request(json.dumps(good).encode(),False),(False,0))
  for value in [True,None,'1794900000000',0,999999999999999]:self.assertEqual(self.run_request(json.dumps(dict(good,epoch_ms=value)).encode()),(False,0))
  self.assertEqual(self.run_request(json.dumps(dict(good,extra=1)).encode()),(False,0));self.assertEqual(self.run_request(b'x'*2049),(False,0));self.assertEqual(self.run_request(json.dumps(dict(good,device_id='cp_0022')).encode()),(False,0))
 def test_each_device_must_match_local_identity(self):
  for ident in ('cp_0017','cp_0022','cp_0023'):
   self.identity.write_text(ident)
   self.assertEqual(self.run_request(json.dumps({'device_id':ident,'epoch_ms':1794900000000,'collection_context':'eeg:1'}).encode()),(True,1))
   self.assertEqual(self.run_request(json.dumps({'device_id':'cp_9999','epoch_ms':1794900000000,'collection_context':'eeg:1'}).encode()),(False,0))
 def test_processing_delay_is_compensated_and_stale_rejected(self):
  received=100_000_000_000;self.assertEqual(m.compensated_epoch(1_800_000_000_000,received,received+9_000_000_000),1_800_000_009.0)
  for invalid in (received-1,received+26_000_000_001):
   with self.assertRaises(ValueError):m.compensated_epoch(1_800_000_000_000,received,invalid)
 def test_preboot_and_lan_lock_refused(self):
  good={'device_id':'cp_0023','epoch_ms':1794900000000,'collection_context':'eeg:1'}
  with patch.object(Guard,'allowed',return_value=(True,'before_application_start')):self.assertEqual(self.run_request(json.dumps(good).encode()),(False,0))
  with self.lock.open('w') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaises(BlockingIOError):self.run_request(json.dumps(good).encode())
 def test_failed_set_restores_old_marker(self):
  good={'device_id':'cp_0023','epoch_ms':1794900000000,'collection_context':'eeg:1','received_monotonic_ns':time.monotonic_ns()};marker=Path(self.tmp.name)/'marker.json';marker.write_text('old-marker');os_open=m.os.open;original=m.Path
  def opening(path,flags,mode=0o777):return os_open(str(self.lock) if path=='/run/headring-lan-time.lock' else path,flags,mode)
  def mapped(path):return self.identity if path=='/opt/egorecord/DEVICE_ID' else original(self.tmp.name)/'pair-hash' if path=='/etc/headring-phone-time.sha256' else marker if path=='/run/headring-phone-time.json' else original(path)
  safe=b'{"collection_context":"eeg:1","state":"device_confirm","privacy":{"required":false,"phase":"idle","stop_confirmed":true}}'
  with patch.object(m,'CaptureGuard',Guard),patch.object(m.os,'open',side_effect=opening),patch.object(m,'Path',side_effect=mapped),patch.object(m.sys,'stdin',Input(json.dumps(good).encode())),patch.object(m.time,'clock_settime',side_effect=ValueError('failed')),patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)),patch.object(m.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(safe)):
   Guard.allow=True
   with self.assertRaises(ValueError):m.main()
   self.assertEqual(marker.read_text(),'old-marker')
 def test_restore_failure_reported(self):
  good={'device_id':'cp_0023','epoch_ms':1794900000000,'collection_context':'eeg:1','received_monotonic_ns':time.monotonic_ns()};os_open=m.os.open;original=m.Path;out=io.StringIO()
  def opening(path,flags,mode=0o777):return os_open(str(self.lock) if path=='/run/headring-lan-time.lock' else path,flags,mode)
  def mapped(path):return self.identity if path=='/opt/egorecord/DEVICE_ID' else original(self.tmp.name)/'pair-hash' if path=='/etc/headring-phone-time.sha256' else original(self.tmp.name)/'marker.json' if path=='/run/headring-phone-time.json' else original(path)
  def run(args,**kwargs):
   if 'start' in args:raise m.subprocess.CalledProcessError(1,args)
   return types.SimpleNamespace(returncode=0)
  safe=b'{"collection_context":"eeg:1","state":"device_confirm","privacy":{"required":false,"phase":"idle","stop_confirmed":true}}'
  with patch.object(m,'CaptureGuard',Guard),patch.object(m.os,'open',side_effect=opening),patch.object(m,'Path',side_effect=mapped),patch.object(m.sys,'stdin',Input(json.dumps(good).encode())),patch.object(m.time,'clock_settime'),patch.object(m.subprocess,'run',side_effect=run),patch.object(m.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(safe)),contextlib.redirect_stdout(out):
   Guard.allow=True;m.main()
  self.assertTrue(json.loads(out.getvalue())['ok']);self.assertFalse(json.loads(out.getvalue())['chrony_restored'])
 def test_chrony_restored_on_busy_race(self):
  good={'device_id':'cp_0023','epoch_ms':1794900000000,'collection_context':'eeg:1','received_monotonic_ns':time.monotonic_ns()};calls=[];os_open=m.os.open;original=m.Path
  def opening(path,flags,mode=0o777):return os_open(str(self.lock) if path=='/run/headring-lan-time.lock' else path,flags,mode)
  def mapped(path):return self.identity if path=='/opt/egorecord/DEVICE_ID' else original(self.tmp.name)/'pair-hash' if path=='/etc/headring-phone-time.sha256' else original(self.tmp.name)/'marker.json' if path=='/run/headring-phone-time.json' else original(path)
  safe=b'{"collection_context":"eeg:1","state":"device_confirm","privacy":{"required":false,"phase":"idle","stop_confirmed":true}}';busy=b'{"collection_context":"eeg:1","state":"recording","privacy":{"required":true,"phase":"idle","stop_confirmed":true}}';responses=iter([safe,busy])
  def run(args,**kwargs):calls.append(args);return types.SimpleNamespace(returncode=0)
  with patch.object(m,'CaptureGuard',Guard),patch.object(m.os,'open',side_effect=opening),patch.object(m,'Path',side_effect=mapped),patch.object(m.sys,'stdin',Input(json.dumps(good).encode())),patch.object(m.time,'clock_settime') as setter,patch.object(m.subprocess,'run',side_effect=run),patch.object(m.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(next(responses))):
   Guard.allow=True
   with self.assertRaises(ValueError):m.main()
   self.assertFalse(setter.called);self.assertTrue(any('stop' in c for c in calls));self.assertTrue(any('start' in c for c in calls))
if __name__=='__main__':unittest.main()
