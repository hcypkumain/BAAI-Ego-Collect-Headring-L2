import ast,io,json,os,stat,sys,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import zip_preview,imports,broker,operations,web_config,fleet
from host.qc.tests.governance_fixtures import sample_registry
TEST_REGISTRY=sample_registry(('cp_0014',))
def fixture_config():
 return operations.bound_config(web_config.setup(registry=TEST_REGISTRY,selected=['cp_0014'],config={'ssh_config':'synthetic'}),TEST_REGISTRY)

def png():
 b=io.BytesIO();Image.new('RGB',(5,6),(50,70,80)).save(b,format='PNG');return b.getvalue()
def archive(entries):
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
  for name,data in entries:z.writestr(name,data)
 b.seek(0);return b
class ZipTests(unittest.TestCase):
 def test_valid_and_hash(self):
  r=zip_preview.preview_file(archive([('任务/图.png',png()),('scene.yaml',b'foo: bar')]))
  self.assertEqual(len(r['images']),1);self.assertEqual(r['images'][0]['width'],5);self.assertEqual(len(r['sha256']),64)
 def test_gb18030_member(self):
  class Info:flag_bits=0;filename='任务.png'.encode('gb18030').decode('cp437')
  self.assertEqual(zip_preview.member_name(Info()),'任务.png')
 def test_traversal_duplicate_and_symlink(self):
  with self.assertRaises(ValueError):zip_preview.preview_file(archive([('../x.png',png())]))
  with self.assertRaises(ValueError):zip_preview.preview_file(archive([('x.png',png()),('x.png',png())]))
  b=io.BytesIO()
  with zipfile.ZipFile(b,'w') as z:
   info=zipfile.ZipInfo('x.png');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(info,png())
  b.seek(0)
  with self.assertRaises(ValueError):zip_preview.preview_file(b)
 def test_encrypted_and_invalid_image(self):
  raw=bytearray(archive([('x.png',png())]).getvalue());pos=raw.index(b'PK\x01\x02');raw[pos+8]|=1
  with self.assertRaises(ValueError):zip_preview.preview_file(io.BytesIO(raw))
  with self.assertRaises(Exception):zip_preview.preview_file(archive([('x.png',b'not image')]))
 def test_corrupt_crc(self):
  raw=bytearray(archive([('x.png',png())]).getvalue());pos=raw.index(b'PK\x01\x02');raw[pos+16]^=0x88
  with self.assertRaises(Exception):zip_preview.preview_file(io.BytesIO(raw))
 def test_pixel_limit(self):
  class Big:
   format='PNG';width=10000;height=10000;size=(10000,10000)
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def load(self):raise AssertionError('must reject before load')
  with patch.object(zip_preview.Image,'open',return_value=Big()):
   with self.assertRaises(ValueError):zip_preview.preview_file(archive([('x.png',png())]))
 def test_unknown_zip_never_remote(self):
  with tempfile.TemporaryDirectory() as t:
   sid='a'*32;state=Path(t);cfg=fixture_config();imports.store(state/'imports/scans'/(sid+'.json'),fleet.bind_registry_plan({'device':'cp_0014','address':cfg['devices']['cp_0014'],'created_at':time.time(),'entries':[]},registry=TEST_REGISTRY,device='cp_0014'))
   with patch.object(imports,'remote') as remote:
    with self.assertRaisesRegex(ValueError,'unknown_zip_id'):imports.preview(state,cfg,'cp_0014',sid,['b'*32])
    remote.assert_not_called()
 def test_broker_operation_scope_separation(self):
  cfg=fixture_config();b=broker.Broker('/tmp',cfg,{'cp_0014':'a'*64},30,b'',operation='import_tasks',registry=TEST_REGISTRY)
  r=b.handle({'operation':'apply_plan','device':'cp_0014','plan_hash':'a'*64,'registry_entry':operations.REGISTRY},os.getuid())
  self.assertEqual(r['error'],'invalid_request');b.clear()
if __name__=='__main__':unittest.main()

class FakeControllerTests(unittest.TestCase):
 def test_adapter_scan_preview_commit_and_readback(self):
  import contextlib,hashlib,urllib.request,types
  code=(ROOT/'zip_preview.py').read_bytes()+b'\n'+(ROOT/'resources/usb_import_adapter.py').read_bytes();ns={'__name__':'adapter_test'};exec(compile(code,'adapter_test','exec'),ns)
  with tempfile.TemporaryDirectory() as temp:
   top=Path(temp);base=top/'project/orangepi';runtime=base/'runtime';catalog_path=runtime/'reference_library/catalog.json';catalog_path.parent.mkdir(parents=True);(runtime/'dashboard_token').write_text('fixture-only')
   (runtime/'usb_task_import').mkdir();(base.parent/'configs/environments').mkdir(parents=True)
   selection={'active_mode':'emg','legacy_mode':'emg','generation':5};(runtime/'collection_profile.json').write_text(json.dumps(selection))
   original={'revision':3,'images':{},'lists':{}};catalog_path.write_text(json.dumps(original));device_path=top/'device_id';device_path.write_text('cp_0014')
   usb=top/'usb';usb.mkdir();path=usb/'task.zip';path.write_bytes(archive([('test.png',png())]).getvalue());st=path.stat();identity=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns];zipid=hashlib.sha256(ns['encoded']([str(path),identity])).hexdigest()[:32];sid='a'*32
   context='emg:5';posted=[];state={'started':False}
   def fake_path(value):
    if str(value)=='/opt/egorecord/DEVICE_ID':return device_path
    if str(value)=='/home/orangepi/Desktop/Embodied-Brain-Collect-main.2/orangepi':return base
    return Path(value)
   def fake_http(req,timeout=0):
    route=req.full_url.split('/api/',1)[1]
    if route=='display-state':v={'state':'idle','busy':False,'session':None,'worker':{},'privacy':{'required':False,'stop_confirmed':True,'phase':'idle'},'collection_context':context,'collection_mode':'emg','revision':7}
    elif route=='phone/usb-task-import/scan':
     posted.append('scan');v={'scan_id':sid,'catalog_revision':3,'files':[{'id':zipid,'relative_path':'task.zip','name':'task.zip','size_bytes':st.st_size}]}
    elif route=='phone/usb-task-import/status':v={'state':'complete' if state['started'] else 'idle','updated_at':2 if state['started'] else 1}
    elif route=='action':
     action=json.loads(req.data);posted.append(action);self.assertEqual(action['value']['remove_list_ids'],[]);state['started']=True
     asset=base.parent/'configs/environments/upload/test.png';asset.parent.mkdir(parents=True);asset.write_bytes(png());digest=hashlib.sha256(png()).hexdigest()
     catalog_path.write_text(json.dumps({'revision':4,'images':{'image_x':{'path':'upload/test.png','sha256':digest,'deleted':False}},'lists':{'new':{'items':['image_x'],'deleted':False}}}))
     v={'accepted':True}
    else:raise AssertionError(route)
    return io.BytesIO(json.dumps(v).encode())
   class Service:stdout=str(base/'scripts/run.sh')
   def call(payload):
    output=io.StringIO()
    with patch.object(sys,'stdin',io.StringIO(json.dumps(payload))),contextlib.redirect_stdout(output):ns['main']()
    return [json.loads(x) for x in output.getvalue().splitlines()]
   with patch.dict(ns,{'Path':fake_path,'mount_roots':lambda:[usb],'snapshot':lambda *args:'f'*64}),patch('urllib.request.urlopen',side_effect=fake_http),patch('subprocess.run',return_value=Service()),patch('time.sleep',return_value=None):
    config=fixture_config()
    def fake_remote(config,device,payload,**kw):
     lines=call(payload)
     for line in lines[:-1]:
      if 'event' in line and kw.get('on_event'):kw['on_event'](line['event'])
     return lines[-1]
    with patch.object(imports,'remote',side_effect=fake_remote):
     scan=imports.scan(top/'local',config,'cp_0014');preview=imports.preview(top/'local',config,'cp_0014',sid,[zipid]);self.assertEqual(preview['new_unique'],1);self.assertEqual(posted,['scan'])
     instance=broker.Broker(top/'local',config,{'cp_0014':preview['plan_sha256']},60,b'',imports.execute,operation='import_tasks',registry=TEST_REGISTRY)
     req={'operation':'import_tasks','device':'cp_0014','plan_hash':preview['plan_sha256'],'registry_entry':imports.REGISTRY}
     apply=instance.handle(req,os.getuid())['result'];self.assertEqual(apply['status'],'succeeded');self.assertTrue(apply['protected_unchanged']);self.assertEqual(len(posted),2);self.assertEqual(posted[-1]['action'],'usb_task_import')
     instance.clear()

class ImportFlowTests(unittest.TestCase):
 def test_plan_broker_fixed_operation_and_one_shot(self):
  cfg=fixture_config();sid='a'*32;zipid='b'*32;calls=[]
  def fake_remote(config,device,payload,**kw):
   calls.append(payload['op'])
   if payload['op']=='scan':return {'status':'ok','mode':'emg','context':'emg:5','scan':{'scan_id':sid,'catalog_revision':4,'files':[{'id':zipid,'relative_path':'task.zip','name':'task.zip','size_bytes':100}]}}
   if payload['op']=='preview':return {'status':'preview','mode':'emg','context':'emg:5','catalog_revision':4,'sources':[{'id':zipid,'sha256':'c'*64,'size':100,'images':[{'sha256':'d'*64}]}],'existing_lists':1,'new_unique':1,'reused_unique':0,'name_conflicts':0,'available_bytes':100000000,'estimated_bytes':40000000,'volume_majmin':'8:1','protected_sha256':'e'*64}
   self.assertEqual(payload['expected']['sources'][0]['sha256'],'c'*64)
   return {'status':'succeeded','revision':5,'verified_unique':1,'old_lists_preserved':True,'protected_unchanged':True}
  with tempfile.TemporaryDirectory() as t,patch.object(imports,'remote',side_effect=fake_remote):
   scan=imports.scan(t,cfg,'cp_0014');plan=imports.preview(t,cfg,'cp_0014',scan['scan_id'],[zipid]);self.assertEqual(calls,['scan','preview'])
   b=broker.Broker(t,cfg,{'cp_0014':plan['plan_sha256']},60,b'',imports.execute,operation='import_tasks',registry=TEST_REGISTRY)
   req={'operation':'import_tasks','device':'cp_0014','plan_hash':plan['plan_sha256'],'registry_entry':imports.REGISTRY}
   events=[];result=b.handle(req,os.getuid(),events.append)
   self.assertEqual(result['result']['status'],'succeeded');self.assertEqual(calls,['scan','preview','apply']);self.assertEqual([x['event'] for x in events],['preflight','applying'])
   again=b.handle(req,os.getuid());self.assertIn('error',again);self.assertEqual(calls,['scan','preview','apply'])
   self.assertNotIn('fixture-only',json.dumps(result));b.clear()

class GuardTests(unittest.TestCase):
 def test_inactive_mode_blocked_and_no_broker_call(self):
  import jobs,broker_client
  cfg=fixture_config()
  with tempfile.TemporaryDirectory() as t:
   owner=jobs.Jobs(t,cfg,registry=TEST_REGISTRY)
   try:
    owner.cache['cp_0014']={'profile_context':{'active_mode':'emg','generation':5},'import_plan':{'plan_sha256':'a'*64,'address':'192.0.2.1','mode':'eeg','context':'eeg:5','created_at':time.time()}}
    with patch.object(broker_client,'status') as status:
     with self.assertRaises(ValueError):owner.import_apply('cp_0014','a'*64,'cp_0014','eeg',True)
     status.assert_not_called()
   finally:owner.lockfile.close()
 def test_drift_and_insufficient_space_no_action(self):
  cfg=fixture_config();sid='a'*32;zipid='b'*32
  with tempfile.TemporaryDirectory() as t:
   imports.store(Path(t)/'imports/scans'/(sid+'.json'),fleet.bind_registry_plan({'device':'cp_0014','address':cfg['devices']['cp_0014'],'created_at':time.time(),'mode':'emg','context':'emg:5','revision':5,'entries':[{'id':zipid,'relative_path':'task.zip'}]},registry=TEST_REGISTRY,device='cp_0014'))
   for reason in ('catalog_changed','insufficient_space','busy_privacy_or_context'):
    with patch.object(imports,'remote',return_value={'status':'failed','reason':reason}) as remote:
     with self.assertRaisesRegex(ValueError,reason):imports.preview(t,cfg,'cp_0014',sid,[zipid])
     self.assertEqual(remote.call_count,1);self.assertEqual(remote.call_args.args[2]['op'],'preview')
 def test_disconnect_after_start_is_not_retried(self):
  cfg=fixture_config()
  with tempfile.TemporaryDirectory() as t:
   path=Path(t)/'imports/plans';plan=fleet.bind_registry_plan({'schema':1,'operation':'import_tasks','device':'cp_0014','address':cfg['devices']['cp_0014'],'created_at':time.time(),'remove_list_ids':[],'scan':'a'*32,'mode':'emg','context':'emg:5','revision':1,'entries':[],'expected':{}},registry=TEST_REGISTRY,device='cp_0014')
   import hashlib
   digest=hashlib.sha256(imports.canonical(plan)).hexdigest();imports.store(path/(digest+'.json'),plan)
   with patch.object(imports,'remote',side_effect=TimeoutError()) as remote:
    self.assertEqual(imports.execute(t,cfg,'cp_0014',digest)['status'],'interrupted_reconcile')
    with self.assertRaises(FileExistsError):imports.execute(t,cfg,'cp_0014',digest)
    self.assertEqual(remote.call_count,1)
 def test_large_body_and_no_upload_endpoint(self):
  import http.client,threading,web,jobs
  from http.server import ThreadingHTTPServer
  with tempfile.TemporaryDirectory() as t:
   owner=jobs.Jobs(t,fixture_config(),registry=TEST_REGISTRY);srv=ThreadingHTTPServer(('127.0.0.1',0),web.Handler);srv.jobs=owner;srv.csrf='t';srv.host='127.0.0.1:%d'%srv.server_port
   thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
   try:
    h={'Host':srv.host,'Origin':'http://'+srv.host,'X-CSRF-Token':'t','Content-Type':'application/json','Content-Length':str(4*1024**3)}
    c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=2);c.request('POST','/api/imports/preview',headers=h);self.assertEqual(c.getresponse().status,400);c.close()
    h['Content-Length']='0';c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=2);c.request('POST','/api/upload',headers=h);self.assertEqual(c.getresponse().status,404);c.close()
   finally:srv.shutdown();srv.server_close();owner.lockfile.close()

class SocketImportTests(unittest.TestCase):
 def test_import_broker_unix_socket_real_plan_with_fake_remote(self):
  import broker_client,threading,socket
  cfg=fixture_config();sid='a'*32;zipid='b'*32;calls=[]
  def fake_remote(config,device,payload,**kw):
   calls.append(payload['op'])
   if payload['op']=='scan':return {'status':'ok','mode':'emg','context':'emg:5','scan':{'scan_id':sid,'catalog_revision':4,'files':[{'id':zipid,'relative_path':'task.zip','name':'task.zip','size_bytes':100}]}}
   if payload['op']=='preview':return {'status':'preview','mode':'emg','context':'emg:5','catalog_revision':4,'sources':[{'id':zipid,'sha256':'c'*64,'size':100,'images':[{'sha256':'d'*64}]}],'existing_lists':1,'new_unique':1,'reused_unique':0,'name_conflicts':0,'available_bytes':100000000,'estimated_bytes':40000000,'volume_majmin':'8:1','protected_sha256':'e'*64}
   kw['on_event']('building:1:1');kw['on_event']('verifying')
   return {'status':'succeeded','revision':5,'verified_unique':1,'old_lists_preserved':True,'protected_unchanged':True}
  with tempfile.TemporaryDirectory() as t,patch.object(imports,'remote',side_effect=fake_remote):
   scan=imports.scan(t,cfg,'cp_0014');plan=imports.preview(t,cfg,'cp_0014',sid,[zipid])
   b=broker.Broker(t,cfg,{'cp_0014':plan['plan_sha256']},60,b'',imports.execute,operation='import_tasks',registry=TEST_REGISTRY);thread=threading.Thread(target=broker.serve,args=(t,b),daemon=True);thread.start()
   try:
    for _ in range(100):
     if (Path(t)/'broker.sock').exists():break
     time.sleep(.01)
    seen=[];req={'operation':'import_tasks','device':'cp_0014','plan_hash':plan['plan_sha256'],'registry_entry':imports.REGISTRY};out=broker_client.call(t,req,seen.append,timeout=5)
    self.assertEqual(out['result']['status'],'succeeded');self.assertEqual(seen,['preflight','applying','building:1:1','verifying']);self.assertEqual(calls,['scan','preview','apply'])
   finally:
    b.stop_requested=True
    try:
     with socket.socket(socket.AF_UNIX) as c:c.connect(str(Path(t)/'broker.sock'))
    except OSError:pass
    thread.join(3)

class CatalogDiffTests(unittest.TestCase):
 def test_duplicate_sha_reuse_conflicts_and_deleted(self):
  code=(ROOT/'zip_preview.py').read_bytes()+b'\n'+(ROOT/'resources/usb_import_adapter.py').read_bytes();ns={'__name__':'adapter_fixture'};exec(compile(code,'adapter_fixture','exec'),ns)
  cat={'images':{'a':{'sha256':'a','original_filename':'x.png','deleted':False},'b':{'sha256':'b','original_filename':'old.png','deleted':True}}}
  zs=[{'images':[{'sha256':'a','name':'x.png'},{'sha256':'b','name':'new.png'}]},{'images':[{'sha256':'a','name':'x.png'},{'sha256':'c','name':'x.png'}]}]
  d=ns['catalog_diff'](cat,zs);self.assertEqual(d['reused_unique'],1);self.assertEqual(d['new_unique'],2);self.assertEqual(d['deleted_conflicts'],1);self.assertGreater(d['name_conflicts'],0)

class HTTPImportTests(unittest.TestCase):
 def test_http_confirmed_import_via_unix_broker(self):
  import http.client,threading,socket,jobs,web
  from http.server import ThreadingHTTPServer
  cfg=fixture_config();sid='a'*32;zid='b'*32;digest='c'*64
  scan={'scan_id':sid,'mode':'emg','context':'emg:5','revision':2,'files':[{'id':zid,'name':'a.zip','size_bytes':100}]}
  plan={'plan_sha256':digest,'device':'cp_0014','address':cfg['devices']['cp_0014'],'mode':'emg','context':'emg:5','created_at':time.time()}
  def execute(state,config,device,hash_value,progress):progress('preflight');progress('applying');progress('verifying');return {'status':'succeeded','revision':3,'protected_unchanged':True}
  with tempfile.TemporaryDirectory() as t,patch.object(imports,'scan',return_value=scan),patch.object(imports,'preview',return_value=plan):
   owner=jobs.Jobs(t,cfg,registry=TEST_REGISTRY);owner.cache['cp_0014']={'profile_context':{'active_mode':'emg','generation':5}}
   b=broker.Broker(t,cfg,{'cp_0014':digest},60,b'',execute,operation='import_tasks',registry=TEST_REGISTRY);bt=threading.Thread(target=broker.serve,args=(t,b),daemon=True);bt.start()
   for _ in range(100):
    if (Path(t)/'broker.sock').exists():break
    time.sleep(.01)
   server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler);server.host='127.0.0.1:%d'%server.server_port;server.jobs=owner;server.csrf='token';threading.Thread(target=server.serve_forever,daemon=True).start()
   def post(path,obj):
    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3);body=json.dumps(obj);c.request('POST',path,body,{'Origin':'http://'+server.host,'X-CSRF-Token':'token','Content-Type':'application/json'});r=c.getresponse();raw=r.read();c.close();self.assertEqual(r.status,202);jid=json.loads(raw)['job']
    for _ in range(300):
     if not owner.guard.locked():break
     time.sleep(.01)
    return owner.jobs[jid]
   try:
    self.assertEqual(post('/api/imports/scan',{'device':'cp_0014'})['status'],'done')
    self.assertEqual(post('/api/imports/preview',{'device':'cp_0014','scan_id':sid,'zip_ids':[zid]})['status'],'done')
    j=post('/api/imports/apply',{'device':'cp_0014','plan_hash':digest,'typed_device':'cp_0014','mode':'emg','keep_old_lists':True});self.assertEqual(j['status'],'succeeded');self.assertTrue(j['receipt']['protected_unchanged'])
   finally:
    server.shutdown();server.server_close();owner.lockfile.close();b.stop_requested=True
    try:
     with socket.socket(socket.AF_UNIX) as c:c.connect(str(Path(t)/'broker.sock'))
    except OSError:pass
    bt.join(3)

class StreamTransportTests(unittest.TestCase):
 def test_stream_events_over_local_fake_ssh(self):
  with tempfile.TemporaryDirectory() as t:
   script=Path(t)/'fake_ssh.py';script.write_text("import sys,json\np=json.load(sys.stdin)\nprint(json.dumps({'event':'building:1:2'}),flush=True)\nprint(json.dumps({'event':'verifying'}),flush=True)\nprint(json.dumps({'status':'succeeded','echo':p['op']}),flush=True)\n")
   class SSH:
    def __init__(self,*args):self.argv=['ssh',args[1]]
   seen=[]
   import subprocess
   popen=subprocess.Popen
   def local(argv,**kwargs):
    self.assertEqual(argv[-2],'test-cp_0014');self.assertIn('stricthostkeychecking=yes',argv)
    return popen([sys.executable,str(script)],**kwargs)
   with patch.object(imports.online,'SSH',SSH),patch.object(imports.online,'config_for',return_value={'user':'fixture','hostkeyalias':'test-key-cp_0014'}),patch.object(imports.subprocess,'Popen',side_effect=local):
    out=imports.remote(fixture_config(),'cp_0014',{'op':'apply'},timeout=10,on_event=seen.append)
   self.assertEqual(out['status'],'succeeded');self.assertEqual(seen,['building:1:2','verifying'])
