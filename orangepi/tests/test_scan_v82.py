import os,sys,tempfile,time,signal,threading,unittest,json,ast,types,subprocess,uuid
from pathlib import Path
from unittest.mock import patch
r=Path(__file__).resolve().parents[1]/'src/orangepi_collect'
sys.path[:0]=[str(r/'common'),str(r/'control'),str(r/'acquisition'),str(r.parent)]
import device_discovery as d
import scan_process as sp
from scan_registry import Registry
from scan_result import encode,decode
from owner_test_support import OwnerTestSupport
import scan_devices as scan

class Tests(unittest.TestCase):
 def setUp(self):
  self.harness=OwnerTestSupport().start();self.addCleanup(self.harness.stop)
 def test_old_inventory_must_wait_for_lock(self):
  cfg={'auto_discovery':{'enabled':True},'wrist_cameras':{'cam_left_wrist':{'device':'/dev/missing','label':'left'}}}
  def candidate(path):return {'kind':'wrist','path':path,'node':path,'vendor':'0bda','product':'5858','serial':'same','topology':'1.2','generation':1}
  entered=threading.Event();resume=threading.Event();new_entered=threading.Event()
  def inventory(_):
   if threading.current_thread().name=='old':entered.set();resume.wait(2);return [candidate('/old')]
   new_entered.set();return [candidate('/new')]
  with tempfile.TemporaryDirectory() as tmp,patch.object(d,'inventory',inventory):
   a=threading.Thread(name='old',target=lambda:d.discover(cfg,tmp));a.start();self.assertTrue(entered.wait(1))
   b=threading.Thread(name='new',target=lambda:d.discover(cfg,tmp));b.start();self.assertFalse(new_entered.wait(.1));resume.set();a.join(2);b.join(2)
   self.assertEqual(json.loads((Path(tmp)/'runtime/device_bindings.json').read_text())['slots']['cam_left_wrist']['path'],'/new')
 def test_successful_scan_releases_durable_registry(self):
  with tempfile.TemporaryDirectory() as tmp:
   registry=Registry(Path(tmp)/'registry')
   result=sp.run_scan([sys.executable,'-c','print("ready")'],registry,timeout=3)
   self.assertEqual(result.strip(),'ready');self.assertIsNone(registry.inspect())
 def test_scan_group_timeout_cleanup(self):
  with tempfile.TemporaryDirectory() as tmp:
   pidpath=Path(tmp)/'child'; f=Path(tmp)/'fd';f.write_text('x')
   code='import sys,subprocess,time; p=subprocess.Popen([sys.executable,"-c","import time,sys;f=open(sys.argv[1]);time.sleep(30)",sys.argv[2]]);open(sys.argv[1],"w").write(str(p.pid));time.sleep(30)'
   with self.assertRaises(sp.ScanProcessError) as cm:sp.run_scan([sys.executable,'-c',code,str(pidpath),str(f)],Registry(Path(tmp)/'registry'),timeout=.5,grace=.3,kill_grace=1)
   self.assertTrue(pidpath.exists());pid=int(pidpath.read_text())
   # Linux may leave a dead zombie until PID 1 reaps it; it cannot retain FDs.
   status=Path('/proc')/str(pid)/'stat';state=status.read_text().split(') ')[1][0] if status.exists() else None
   self.assertIn(state,(None,'Z'))
   print('cleanup_pending on this host:',cm.exception.cleanup_pending)
 def test_timeout_kills_owned_leader_and_grandchild_and_releases_fds(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);data=root/'held';data.write_text('ordinary test file');meta=root/'grandchild.json';leader=root/'leader.json'
   grand="import os,signal,time,sys,json;signal.signal(signal.SIGTERM,signal.SIG_IGN);f=open(sys.argv[1]);open(sys.argv[2],'w').write(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0)}));time.sleep(30)"
   code="import os,signal,time,sys,json,subprocess;signal.signal(signal.SIGTERM,signal.SIG_IGN);f=open(sys.argv[1]);open(sys.argv[3],'w').write(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0)}));subprocess.Popen([sys.executable,'-c',sys.argv[4],sys.argv[1],sys.argv[2]]);time.sleep(30)"
   with self.assertRaises(sp.ScanProcessError):sp.run_scan([sys.executable,'-c',code,str(data),str(meta),str(leader),grand],Registry(root/'registry'),timeout=.7,grace=.2,kill_grace=1)
   a=json.loads(leader.read_text());b=json.loads(meta.read_text());self.assertEqual((b['pgid'],b['sid']),(a['pid'],a['pid']))
   for pid in (a['pid'],b['pid']):
    proc=Path('/proc')/str(pid);stat=proc/'stat';state=stat.read_text().split(') ')[1][0] if stat.exists() else None
    self.assertIn(state,(None,'Z'))
    if proc.exists() and (proc/'fd').exists():self.assertEqual(list((proc/'fd').iterdir()),[])
 def test_unreaped_process_is_not_success(self):
  with tempfile.TemporaryDirectory() as tmp:
   with patch.object(sp,'group_alive',return_value=True),patch.object(sp.os,'killpg') as signal_group:
    with self.assertRaises(sp.ScanProcessError) as cm:sp.run_scan([sys.executable,'-c','pass'],Registry(Path(tmp)/'registry'),timeout=1,grace=.01,kill_grace=.01)
    signal_group.assert_not_called()
   self.assertTrue(cm.exception.cleanup_pending)
 def test_status_ready_not_frame_ready(self):
  item={'label':'head'}
  with patch.object(scan,'camd_camera_status',return_value={'snapshot_ready':True,'fps':30}),patch.object(scan.camd_source,'snapshot',side_effect=TimeoutError('no frame')):
   value=scan.check('camera','cam_ros_bleft',item)
  self.assertTrue(value['present']);self.assertFalse(value['connected']);self.assertEqual(value['status'],'error');self.assertTrue(value['service_ready']);self.assertFalse(value['fresh_frame'])
 def test_one_slow_probe_preserves_three_completed(self):
  with tempfile.TemporaryDirectory() as tmp:
   fake=Path(tmp)/'fake.py'
   fake.write_text("import sys,time,json\nk,n,i=json.load(sys.stdin)\nif n=='slow':time.sleep(10)\nprint(json.dumps(dict(name=n,label=n,connected=True,present=True,status='ready')),flush=True)\n")
   jobs=[('camera',n,{'label':n}) for n in ('a','b','c','slow')]
   with patch.object(scan,'__file__',str(fake)):
    started=time.monotonic();rows=scan.probe_rows(jobs,timeout=.4)
   self.assertLess(time.monotonic()-started,1.5)
   self.assertEqual([x['connected'] for x in rows],[True,True,True,False]);self.assertEqual(rows[-1]['status'],'timeout')
 def test_result_from_changed_generation_is_not_ready(self):
  path=r/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('scan_devices','_scan_devices')]
  fingerprints=iter([('video1',1),('video1',2)])
  scope=dict(uuid=uuid,os=os,decode_scan_result=decode,fingerprint=lambda *_:next(fingerprints),devices=lambda:{},run_scan=lambda *a,**kw:(encode('run','nonce',json.loads(kw['env']['ORANGEPI_SCAN_REQUEST'])['id'],json.loads(kw['env']['ORANGEPI_SCAN_REQUEST'])['fingerprint'],[dict(name='wrist',label='A',connected=True,status='ready')]),'run','nonce'),ScanProcessError=sp.ScanProcessError,atomic_json=lambda *a:None,BASE=Path('/tmp'),SRC=Path('/tmp'),json=json,subprocess=subprocess,time=time)
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.camera_registry=object();c.scan_lock=threading.Lock();c.worker=None;c.state='idle';c.last_scan=[];c.stop_preview=lambda:None;c.state_write=lambda:None;c.voice=types.SimpleNamespace(say=lambda *_:None)
  self.assertFalse(c.scan_devices(quiet=True));self.assertFalse(c.scan_ok);self.assertEqual(c.scan_available,[]);self.assertTrue(c.scan_retry_pending)
 def test_timeout_partial_results_return_after_group_reaped(self):
  with tempfile.TemporaryDirectory() as tmp:
   code="import json,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(json.dumps([dict(name='good',label='A',connected=True),dict(name='slow',label='B',connected=False,status='timeout')]),flush=True);time.sleep(30)"
   text=sp.run_scan([sys.executable,'-c',code],Registry(Path(tmp)/'registry'),timeout=.4,grace=.1,kill_grace=1,allow_partial=True)
   rows=json.loads(text);self.assertTrue(rows[0]['connected']);self.assertEqual(rows[1]['status'],'timeout')
 def test_pending_group_retains_partial_evidence_without_ready(self):
  path=r/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('scan_devices','_scan_devices')]
  def runner(*args,**kwargs):
   req=json.loads(kwargs['env']['ORANGEPI_SCAN_REQUEST'])
   partial=encode('run','nonce',req['id'],req['fingerprint'],[dict(name='good',label='A',connected=True,status='ready'),dict(name='slow',label='B',connected=False,status='timeout')])
   error=sp.ScanProcessError('pending',True,123,'run',None,partial);error.nonce='nonce';raise error
  scope=dict(uuid=uuid,os=os,decode_scan_result=decode,fingerprint=lambda *_:('stable',),devices=lambda:{},run_scan=runner,ScanProcessError=sp.ScanProcessError,atomic_json=lambda *a:None,BASE=Path('/tmp'),SRC=Path('/tmp'),json=json,subprocess=subprocess,time=time)
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.remember_pending_owner=lambda e:None;c.camera_registry=object();c.scan_lock=threading.Lock();c.worker=None;c.state='idle';c.last_scan=[];c.stop_preview=lambda:None;c.state_write=lambda:None;c.voice=types.SimpleNamespace(say=lambda *_:None)
  self.assertFalse(c.scan_devices(quiet=True));self.assertFalse(c.scan_ok);self.assertEqual(c.scan_available,[]);self.assertTrue(c.last_scan[0]['last_probe_connected']);self.assertFalse(c.last_scan[0]['connected'])
 def test_three_fresh_one_timeout_survive_owned_group_cleanup(self):
  import ctypes
  libc=ctypes.CDLL(None,use_errno=True);old=ctypes.c_int()
  if libc.prctl(37,ctypes.byref(old),0,0,0)!=0 or libc.prctl(36,1,0,0,0)!=0:self.skipTest('subreaper unavailable')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);pidfile=root/'grandchild';request={'id':'req123','fingerprint':[]};env=dict(os.environ,ORANGEPI_SCAN_REQUEST=json.dumps(request),PYTHONPATH=str(r/'common'))
   grand="import os,signal,sys,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
   code="import os,sys,time,json,signal,subprocess,pathlib;from scan_registry import Registry;from scan_result import encode;signal.signal(signal.SIGTERM,signal.SIG_IGN);p=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);f=pathlib.Path(sys.argv[1]);\nwhile not f.exists():time.sleep(.01)\no=Registry(os.environ['ORANGEPI_CAMERA_OWNER_ROOT']).inspect();q=json.loads(os.environ['ORANGEPI_SCAN_REQUEST']);rows=[dict(name='cam_ros_'+n,label=n,connected=True,status='ready',fresh_frame=True,probe_scope='camd_snapshot') for n in ('left','right','bright')]+[dict(name='cam_ros_bleft',label='bleft',connected=False,status='timeout')];print(encode(o['run'],o['nonce'],q['id'],q['fingerprint'],rows),flush=True);time.sleep(30)"
   def reap_grand():
    until=time.monotonic()+4
    while time.monotonic()<until:
     if pidfile.exists():
      try:
       done,_=os.waitpid(int(pidfile.read_text()),os.WNOHANG)
       if done:return
      except ChildProcessError:pass
     time.sleep(.01)
   thread=threading.Thread(target=reap_grand);thread.start()
   try:
    output,run,nonce=sp.run_scan([sys.executable,'-c',code,str(pidfile),grand],Registry(root/'registry'),timeout=.7,grace=.15,kill_grace=1.5,allow_partial=True,env=env,return_identity=True)
    rows=decode(output,run,nonce,request['id'],request['fingerprint']);self.assertEqual([row['connected'] for row in rows],[True,True,True,False]);self.assertEqual(rows[-1]['status'],'timeout')
   finally:
    if pidfile.exists():
     try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
     except ProcessLookupError:pass
    thread.join(4);libc.prctl(36,old.value,0,0,0)
 def test_late_output_after_controller_cancel_is_not_published(self):
  path=r/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name in ('scan_devices','_scan_devices')];writes=[]
  c=None
  def runner(*args,**kwargs):
   c.quit=True;req=json.loads(kwargs['env']['ORANGEPI_SCAN_REQUEST'])
   return encode('run','nonce',req['id'],req['fingerprint'],[dict(name='wrist',label='A',connected=True,status='ready')]),'run','nonce'
  scope=dict(uuid=uuid,os=os,decode_scan_result=decode,fingerprint=lambda *_:('stable',),devices=lambda:{},run_scan=runner,ScanProcessError=sp.ScanProcessError,atomic_json=lambda *args:writes.append(args),BASE=Path('/tmp'),SRC=Path('/tmp'),json=json,subprocess=subprocess,time=time)
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope);c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.camera_registry=object();c.scan_lock=threading.Lock();c.worker=None;c.state='idle';c.last_scan=[{'name':'previous'}];c.stop_preview=lambda:None;c.state_write=lambda:None;c.voice=types.SimpleNamespace(say=lambda *_:None)
  self.assertFalse(c.scan_devices(quiet=True));self.assertEqual(c.last_scan,[{'name':'previous'}]);self.assertEqual(writes,[])
 def test_descendant_inheriting_stdout_never_blocks_wait(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);pidfile=root/'pid';code="import sys,subprocess,pathlib; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);pathlib.Path(sys.argv[1]).write_text(str(p.pid));print('partial',flush=True)"
   started=time.monotonic()
   try:
    with self.assertRaises(sp.ScanProcessError) as result:sp.run_scan([sys.executable,'-c',code,str(pidfile)],Registry(root/'registry'),timeout=1,grace=.1,kill_grace=.1)
    self.assertTrue(result.exception.cleanup_pending);self.assertLess(time.monotonic()-started,2)
   finally:
    if pidfile.exists():
     try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
     except ProcessLookupError:pass
 def test_scan_refused_when_worker_active(self):
  path=r/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='scan_devices'];scope={}
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.scan_lock=threading.Lock();c.worker=object();c.state='ready'
  self.assertFalse(c.scan_devices());self.assertTrue(c.last_help)
 def test_scans_rejected_during_active_states_and_when_locked(self):
  path=r/'control/launcher.py';tree=ast.parse(path.read_text());cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='Controller')
  cls.bases=[];cls.body=[x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='scan_devices'];scope={}
  exec(compile(ast.Module(body=[cls],type_ignores=[]),str(path),'exec'),scope)
  c=scope['Controller']();c.retry_pending_owner=lambda **kw:True;c.scan_lock=threading.Lock();c.worker=None
  for state in ('preparing','recording','countdown','finalizing','confirm_stop'):
   c.state=state;self.assertFalse(c.scan_devices())
  c.state='idle';c.scan_lock.acquire()
  try:self.assertFalse(c.scan_devices())
  finally:c.scan_lock.release()
 def test_same_serial_distinct_paths(self):
  a={'kind':'wrist','path':'/path/a','node':'/dev/video1','vendor':'v','product':'p','serial':'same','topology':'1.2','generation':1}
  b=dict(a,path='/path/b',node='/dev/video2',topology='1.3')
  cfg={'wrist_cameras':{'cam_left_wrist':{'device':'/path/a'},'cam_right_wrist':{'device':'/path/b'}}}
  resolved,_=d.resolve(cfg,[a,b],{})
  self.assertEqual(resolved['wrist_cameras']['cam_left_wrist']['device'],'/path/a');self.assertEqual(resolved['wrist_cameras']['cam_right_wrist']['device'],'/path/b')
if __name__=='__main__':unittest.main(verbosity=2)
