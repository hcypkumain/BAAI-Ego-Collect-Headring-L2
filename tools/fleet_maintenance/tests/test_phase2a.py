import http.client,json,os,socket,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import broker,broker_client,jobs,operations,online,fleet,web,web_config
from host.qc.tests.governance_fixtures import sample_registry

class Phase2Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)
  self.registry=sample_registry(('cp_0014',));self.config=web_config.setup(registry=self.registry,selected=['cp_0014'],config={'ssh_config':'unused'});self.digest='a'*64
 def instance(self,executor=None,ttl=10):
  return broker.Broker(self.path,self.config,{'cp_0014':self.digest},ttl,b'never-log-secret',executor or (lambda *a,**kw:{'status':'succeeded'}),registry=self.registry)
 def server(self,inst):
  thread=threading.Thread(target=broker.serve,args=(self.path,inst),daemon=True);thread.start()
  for _ in range(100):
   if (self.path/'broker.sock').exists(): break
   time.sleep(.01)
  self.assertTrue((self.path/'broker.sock').exists())
  def stop():
   inst.failed=True
   try:
    with socket.socket(socket.AF_UNIX) as s:s.connect(str(self.path/'broker.sock'))
   except OSError:pass
   thread.join(3)
  self.addCleanup(stop)
 def test_scope_expiry_uid_and_secret_redaction(self):
  b=self.instance();request={'operation':'apply_plan','device':'cp_0014','plan_hash':self.digest,'registry_entry':operations.REGISTRY}
  self.assertEqual(b.handle(request,os.getuid()+1)['error'],'forbidden')
  self.assertEqual(b.handle({**request,'plan_hash':'b'*64},os.getuid())['error'],'locked_or_out_of_scope')
  b.deadline=time.monotonic()-1;self.assertFalse(b.status()['unlocked']);self.assertNotIn('never-log-secret',json.dumps(b.status()));b.clear();self.assertFalse(any(b.secret))
 def test_real_socket_fake_http_job_end_to_end(self):
  def fake(state,config,device,digest,factory,progress,*,registry):
   progress('preflight');progress('applying');progress('verifying');return {'status':'succeeded','code':'verified','components':'root_transaction_verified'}
  b=self.instance(fake);self.server(b)
  owner=jobs.Jobs(self.path,self.config,registry=self.registry);self.addCleanup(owner.lockfile.close)
  owner.cache['cp_0014']={'address':'192.0.2.1','review_plan':{'plan_sha256':self.digest,'address':'192.0.2.1','created_at':time.time()}}
  srv=ThreadingHTTPServer(('127.0.0.1',0),web.Handler);srv.host='127.0.0.1:%d'%srv.server_port;srv.csrf='csrf';srv.jobs=owner
  threading.Thread(target=srv.serve_forever,daemon=True).start();self.addCleanup(srv.server_close);self.addCleanup(srv.shutdown)
  body=json.dumps({'device':'cp_0014','typed_device':'cp_0014','plan_hash':self.digest,'registry_entry':operations.REGISTRY})
  h={'Origin':'http://'+srv.host,'X-CSRF-Token':'csrf','Content-Type':'application/json'}
  with patch.object(broker.web_config,'setup',return_value=self.config):
   conn=http.client.HTTPConnection('127.0.0.1',srv.server_port);conn.request('POST','/api/operations/apply',body,h);r=conn.getresponse();self.assertEqual(r.status,202);jid=json.loads(r.read())['job'];conn.close()
   for _ in range(200):
    if owner.jobs[jid]['status'] in ('succeeded','failed','partial_failed'):break
    time.sleep(.01)
   self.assertEqual(owner.jobs[jid]['status'],'succeeded');self.assertEqual(owner.jobs[jid]['events'],['queued','preflight','applying','verifying','succeeded'])
  for path in self.path.glob('*.json'): self.assertNotIn('never-log-secret',path.read_text())
 def test_actual_operation_fake_ssh_socket_path(self):
  plan=fleet.bind_registry_plan({'device':'cp_0014','commit':fleet.PIN,'unknown_approved':False},registry=self.registry,device='cp_0014');(self.path/'plans').mkdir();(self.path/'plans'/(self.digest+'.json')).write_text('{}')
  b=self.instance(operations.execute);self.server(b);owner=jobs.Jobs(self.path,self.config,registry=self.registry);self.addCleanup(owner.lockfile.close)
  owner.cache['cp_0014']={'address':'192.0.2.1','review_plan':{'plan_sha256':self.digest,'address':'192.0.2.1','created_at':time.time()}}
  live={'files':{},'component_timer':{}}
  with patch.object(broker.web_config,'setup',return_value=self.config),patch.object(online,'load_plan',return_value=plan),patch.object(fleet,'build',return_value={}),patch.object(online,'read_live',return_value=live),patch.object(online,'matches'),patch.object(online,'component_diff',return_value=[]),patch.object(online,'stage_and_apply',return_value='fixture-receipt') as stage,patch.object(online,'verify',return_value={'deploy_ready':True}) as verify,patch.object(broker,'AuthSSH',return_value=object()):
   jid=owner.apply('cp_0014',self.digest,'cp_0014',operations.REGISTRY)
   for _ in range(300):
    if owner.jobs[jid]['status'] in ('succeeded','failed','partial_failed'):break
    time.sleep(.01)
   self.assertEqual(owner.jobs[jid]['status'],'succeeded');stage.assert_called_once();verify.assert_called_once();self.assertTrue(verify.call_args.kwargs['root_components_verified'])
 def test_actual_staging_fake_ssh_end_to_end(self):
  (self.path/'plans').mkdir();(self.path/'plans'/(self.digest+'.json')).write_text('{}')
  code=self.path/'code.tar';code.write_bytes(b'fake-code');components=self.path/'components.tar';components.write_bytes(b'fake-components')
  bundle={'bundle':str(code),'bundle_sha256':fleet.sha(code.read_bytes()),'component_bundle':str(components),'component_bundle_sha256':fleet.sha(components.read_bytes()),'manifest_sha256':fleet.MANIFEST_SHA256}
  plan={'device':'cp_0014','commit':fleet.PIN,'unknown_approved':False,'baseline':[],'tool_sha256':fleet.sha(online.TOOL.read_bytes()),'component_tool_sha256':fleet.sha(online.COMPONENT_TOOL.read_bytes()),'component_bundle_sha256':bundle['component_bundle_sha256'],'component_manifest_sha256':'b'*64}
  plan=fleet.bind_registry_plan(plan,registry=self.registry,device='cp_0014')
  calls=[]
  class FakeSSH:
   def __init__(self,*args,**kwargs): pass
   def require_tty(self):pass
   def run(self,command,data=None,interactive=False):
    calls.append((command,data,interactive))
    if command.startswith('umask 077; mktemp'):return b'/home/orangepi/.headring-stage-AbCd1234\n'
    return b''
  b=self.instance(operations.execute);self.server(b);owner=jobs.Jobs(self.path,self.config,registry=self.registry);self.addCleanup(owner.lockfile.close)
  owner.cache['cp_0014']={'address':'192.0.2.1','review_plan':{'plan_sha256':self.digest,'address':'192.0.2.1','created_at':time.time()}}
  live={'files':{},'component_timer':{}}
  with patch.object(broker.web_config,'setup',return_value=self.config),patch.object(online,'load_plan',return_value=plan),patch.object(fleet,'build',return_value=bundle),patch.object(online,'read_live',return_value=live),patch.object(online,'matches'),patch.object(online,'component_diff',return_value=[]),patch.object(online,'verify',return_value={'deploy_ready':True}),patch.object(broker,'AuthSSH',FakeSSH):
   jid=owner.apply('cp_0014',self.digest,'cp_0014',operations.REGISTRY)
   for _ in range(300):
    if owner.jobs[jid]['status'] in ('succeeded','failed','partial_failed'):break
    time.sleep(.01)
   self.assertEqual(owner.jobs[jid]['status'],'succeeded');self.assertEqual(sum(x[2] for x in calls),1);self.assertTrue(any('remote_components.py' in x[0] and x[2] for x in calls));self.assertEqual(sum(x[1] is not None for x in calls),7)
 def test_partial_failure_not_green(self):
  def fake(state,config,device,digest,factory,progress,*,registry):
   progress('preflight');progress('applying');raise operations.PartialFailure('component failed')
  b=self.instance(fake);self.server(b);owner=jobs.Jobs(self.path,self.config,registry=self.registry);self.addCleanup(owner.lockfile.close)
  owner.cache['cp_0014']={'address':'192.0.2.1','review_plan':{'plan_sha256':self.digest,'address':'192.0.2.1','created_at':time.time()}}
  with patch.object(broker.web_config,'setup',return_value=self.config):
   jid=owner.apply('cp_0014',self.digest,'cp_0014',operations.REGISTRY)
   for _ in range(200):
    if owner.jobs[jid]['status'] in ('partial_failed','failed'):break
    time.sleep(.01)
   self.assertEqual(owner.jobs[jid]['status'],'partial_failed');self.assertFalse(b.allowed())
 def test_http_locked_423(self):
  owner=jobs.Jobs(self.path,self.config,registry=self.registry);self.addCleanup(owner.lockfile.close)
  owner.cache['cp_0014']={'address':'192.0.2.1','review_plan':{'plan_sha256':self.digest,'address':'192.0.2.1','created_at':time.time()}}
  srv=ThreadingHTTPServer(('127.0.0.1',0),web.Handler);srv.host='127.0.0.1:%d'%srv.server_port;srv.csrf='t';srv.jobs=owner
  threading.Thread(target=srv.serve_forever,daemon=True).start();self.addCleanup(srv.server_close);self.addCleanup(srv.shutdown)
  body=json.dumps({'device':'cp_0014','typed_device':'cp_0014','plan_hash':self.digest,'registry_entry':operations.REGISTRY})
  c=http.client.HTTPConnection('127.0.0.1',srv.server_port);c.request('POST','/api/operations/apply',body,{'Origin':'http://'+srv.host,'X-CSRF-Token':'t','Content-Type':'application/json'});self.assertEqual(c.getresponse().status,423);c.close()
 def test_expiry_during_preflight_blocks_apply(self):
  called=[]
  def fake(state,config,device,digest,factory,progress,*,registry):
   progress('preflight'); b.deadline=time.monotonic()-1;progress('applying');called.append('root')
  b=self.instance(fake)
  req={'operation':'apply_plan','device':'cp_0014','plan_hash':self.digest,'registry_entry':operations.REGISTRY}
  with patch.object(broker.web_config,'setup',return_value=self.config):
   self.assertEqual(b.handle(req,os.getuid())['error'],'preflight_or_auth_failed')
  self.assertEqual(called,[])
 def test_second_broker_never_removes_first_socket(self):
  first=self.instance();self.server(first);other=self.instance()
  with self.assertRaises(BlockingIOError):broker.serve(self.path,other)
  self.assertTrue((self.path/'broker.sock').is_socket());self.assertTrue(broker_client.status(self.path)['unlocked'])
 def test_foreign_socket_not_removed(self):
  path=self.path/'broker.sock';path.write_text('foreign');b=self.instance()
  with self.assertRaises(OSError):broker.serve(self.path,b)
  self.assertEqual(path.read_text(),'foreign')
 def test_reconcile_on_restart(self):
  (self.path/'jobs.json').write_text(json.dumps({'x':{'kind':'apply','status':'applying'}}));o=jobs.Jobs(self.path,self.config,registry=self.registry)
  try:self.assertEqual(o.jobs['x']['status'],'interrupted_reconcile')
  finally:o.lockfile.close()
 def test_fixed_operation_drift_never_stages_and_partial(self):
  plan=fleet.bind_registry_plan({'device':'cp_0014','commit':fleet.PIN,'unknown_approved':False},registry=self.registry,device='cp_0014');(self.path/'plans').mkdir();(self.path/'plans'/(self.digest+'.json')).write_text('{}')
  manifest=json.loads((fleet.RESOURCES/'manifest.json').read_text());live={'files':{},'component_timer':{}}
  common=[patch.object(online,'load_plan',return_value=plan),patch.object(fleet,'build',return_value={}),patch.object(online,'read_live',return_value=live),patch.object(online,'component_diff',return_value=[])]
  for p in common:p.start();self.addCleanup(p.stop)
  with patch.object(online,'matches',side_effect=online.Failure('drift')),patch.object(online,'stage_and_apply') as stage:
   with self.assertRaises(online.Failure):operations.execute(self.path,self.config,'cp_0014',self.digest,lambda *a:object(),registry=self.registry)
   stage.assert_not_called()
  with patch.object(online,'matches'),patch.object(online,'stage_and_apply',side_effect=online.Failure('component failure')):
   with self.assertRaises(operations.PartialFailure):operations.execute(self.path,self.config,'cp_0014',self.digest,lambda *a:object(),registry=self.registry)
 def test_skip_current_no_restart(self):
  plan=fleet.bind_registry_plan({'device':'cp_0014','commit':fleet.PIN,'unknown_approved':False},registry=self.registry,device='cp_0014');(self.path/'plans').mkdir();(self.path/'plans'/(self.digest+'.json')).write_text('{}')
  manifest=json.loads((fleet.RESOURCES/'manifest.json').read_text());live={'files':{x['path']:{'sha256':x['sha256']} for x in manifest['files']},'component_timer':{'is-enabled':'enabled','is-active':'active'}}
  with patch.object(online,'load_plan',return_value=plan),patch.object(fleet,'build',return_value={}),patch.object(online,'read_live',return_value=live),patch.object(online,'component_diff',return_value=[{'status':'same'}]*5),patch.object(online,'matches'),patch.object(online,'stage_and_apply') as stage:
   r=operations.execute(self.path,self.config,'cp_0014',self.digest,lambda *a:object(),registry=self.registry);self.assertEqual(r['status'],'skipped_current');stage.assert_not_called()
if __name__=='__main__':unittest.main()

class AuthTransportTests(unittest.TestCase):
 def test_sudo_prompt_once_without_output_logging(self):
  from unittest.mock import patch
  class Child:
   logfile='not-set'; exitstatus=0
   def __init__(self): self.i=0;self.sent=[]
   def expect(self,patterns):self.i+=1;return 0 if self.i==1 else 3
   def sendline(self,value):self.sent.append(value)
   def close(self,**kw):pass
   def isalive(self):return False
  child=Child(); ssh=object.__new__(broker.AuthSSH);ssh.argv=['ssh','-F','cfg','cp_0014'];ssh.secret=bytearray(b'local-only-secret')
  with patch('pexpect.spawn',return_value=child): self.assertEqual(ssh.run('sudo id',interactive=True),b'')
  self.assertIsNone(child.logfile);self.assertEqual(child.sent,['local-only-secret'])
 def test_repeat_prompt_fails_closed(self):
  from unittest.mock import patch
  class Child:
   logfile=None;exitstatus=1
   def expect(self,patterns):return 0
   def sendline(self,value):pass
   def close(self,**kw):pass
   def isalive(self):return False
  ssh=object.__new__(broker.AuthSSH);ssh.argv=['ssh','cp_0014'];ssh.secret=bytearray(b'x')
  with patch('pexpect.spawn',return_value=Child()):
   with self.assertRaises(online.Failure):ssh.run('sudo id',interactive=True)
