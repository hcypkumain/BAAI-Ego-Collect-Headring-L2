import contextlib, copy, io, json, pathlib, shutil, sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import delta_release as d
import fleet, online
from dataclasses import asdict
from host.qc.tests.governance_fixtures import sample_registry

class DeltaReleaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.state=pathlib.Path(self.tmp.name)
  self.manifest,self.stable,_,_=d.registered()
  self.registry=sample_registry(('cp_0022',));self.registry_path=self.state/'registry.json'
  self.registry_path.write_text(json.dumps({'schema_version':1,'devices':[asdict(x) for x in self.registry.devices]}))
  patcher=patch.object(online,'config_for',return_value={'user':'fixture','hostkeyalias':'test-key-cp_0022'})
  patcher.start();self.addCleanup(patcher.stop)
 def live(self,after=False):
  overlay={x['path']:x['sha256'] for x in self.manifest['files']}
  return {'device_id':'cp_0022','project':online.PROJECT,'errors':[],
   'api':{'state':'device_confirm','busy':False,'session':None,'privacy':{'required':False,'phase':'idle','stop_confirmed':True,'session_id':''}},
   'services':{'embodied-pi-collect':'active','camd':'active'},'capture_workers':[],
   'capture_locks':[{'result':'free'}],'protected':{'config':{'sha256':'same'}},'camd_pid':'42',
   'files':{x['path']:{'type':'regular','sha256':overlay.get(x['path'],x['sha256']) if after else x['sha256']} for x in self.stable['files']}}
 def test_registered_exact_5_and_archive_hash(self):
  self.assertEqual(len(self.manifest['files']),5)
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp)/'resource';shutil.copytree(d.RESOURCE,root)
   (root/'bundle.zip').write_bytes(b'tamper')
   with patch.object(d,'RESOURCE',root), self.assertRaises(online.Failure):d.registered()
 def test_invalid_path_and_before_after_rejected(self):
  bad=copy.deepcopy(self.manifest);bad['files'][0]['path']='etc/passwd'
  with self.assertRaises((online.Failure,KeyError)):d.classify(self.live(),self.stable,bad)
  for key in ('before_sha256','sha256'):
   bad=copy.deepcopy(self.manifest);bad['files'][0][key]='0'*64
   target=self.live(after=key=='sha256')
   with self.assertRaises(online.Failure):d.classify(target,self.stable,bad)
 def test_before_after_only_and_mixed_rejected(self):
  self.assertEqual(d.classify(self.live(),self.stable,self.manifest),'before')
  self.assertEqual(d.classify(self.live(True),self.stable,self.manifest),'after')
  live=self.live();item=self.manifest['files'][0];live['files'][item['path']]['sha256']=item['sha256']
  with self.assertRaises(online.Failure):d.classify(live,self.stable,self.manifest)
  with self.assertRaises(online.Failure):d.refuse_stable_overlay(live)
  with self.assertRaises(online.Failure):online.prepare_plan(live,self.stable,'cp_0022',{'manifest_sha256':fleet.MANIFEST_SHA256},True)
 def test_cached_known_host_and_failed_override_never_saved(self):
  output=io.StringIO()
  with patch.object(online,'SSH') as ssh, patch.object(online,'read_live',side_effect=online.Failure('offline')), contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
   with self.assertRaises(SystemExit) as failure:
    fleet.main(['--registry',str(self.registry_path),'--release','eeg-impedance-advisory','--targets','cp_0022','--address','cp_0022=192.0.2.199','--state-dir',str(self.state)])
  self.assertEqual(failure.exception.code,2);self.assertFalse((self.state/'inventory.json').exists())
  self.assertFalse((self.state/'observations.json').exists());ssh.assert_not_called()
  self.assertEqual(self.registry.devices[0].address,'192.0.2.1')
  self.assertNotIn('password',output.getvalue().lower())
 def test_offline_plan_no_network_and_only_selected(self):
  with patch.object(online,'SSH',side_effect=AssertionError('network forbidden')),contextlib.redirect_stdout(io.StringIO()):
    self.assertEqual(fleet.main(['--registry',str(self.registry_path),'--release',d.RELEASE,'--offline-plan','--targets','cp_0022','--state-dir',str(self.state)]),0)
 def test_fake_plan_apply_and_already_installed_skip(self):
  before=self.live();after=self.live(True);ssh=unittest.mock.Mock();profile_before={'source':online.PROJECT+'/orangepi/src','policy':'strict','explicit_policy':None}
  profile_after=dict(profile_before,policy='advisory'); out=io.StringIO()
  cli=['--registry',str(self.registry_path),'--release',d.RELEASE,'--targets','cp_0022','--state-dir',str(self.state)]
  ssh.argv=['ssh','-F','synthetic','test-cp_0022']
  with patch.object(online,'SSH',return_value=ssh), \
       patch.object(online,'read_live',side_effect=[before,before,after,after,after]), \
       patch.object(d,'read_profile',side_effect=[profile_before,profile_before,profile_after,profile_after,profile_after]), \
       patch.object(d,'stage_apply',return_value={'status':'applied'}) as apply,contextlib.redirect_stdout(out):
   self.assertEqual(fleet.main(cli),0);plan=json.loads(out.getvalue())['plan_sha256'];out.seek(0);out.truncate()
   self.assertEqual(fleet.main(cli+['--apply','--accepted-plan',plan]),0);self.assertEqual(apply.call_count,1)
   out.seek(0);out.truncate();self.assertEqual(fleet.main(cli),0);plan2=json.loads(out.getvalue())['plan_sha256'];out.seek(0);out.truncate()
   self.assertEqual(fleet.main(cli+['--apply','--accepted-plan',plan2]),0);self.assertEqual(json.loads(out.getvalue())['status'],'already_installed_verified');self.assertEqual(apply.call_count,1)
 def test_plan_release_not_stable(self):
  plan={'device':'cp_0022','commit':fleet.PIN,'manifest_sha256':d.MANIFEST_SHA,'release':d.RELEASE}
  live=self.live()
  with self.assertRaises(online.Failure):online.matches(plan,live,'cp_0022',self.stable,{'manifest_sha256':fleet.MANIFEST_SHA256})

 def test_staging_command_is_registered_password_free_and_no_remote_tty(self):
  class FakeSSH:
   argv=['ssh','cp_0022']
   def __init__(self):self.commands=[]
   def run(self,command,data=None,interactive=False):
    self.commands.append((command,data,interactive))
    if 'mktemp' in command:return b'/home/orangepi/.headring-stage-AbCd1234\n'
    return b''
   def require_tty(self):pass
  ssh=FakeSSH();_,_,bundle,bootstrap=d.registered()
  plan=fleet.bind_registry_plan({'device':'cp_0022'},registry=self.registry,device='cp_0022')
  result=d.stage_apply(ssh,'cp_0022',bundle,bootstrap,registry=self.registry,plan=plan)
  self.assertEqual(result['status'],'applied');self.assertEqual(ssh.commands[1][1],bundle)
  command=ssh.commands[-1][0];self.assertTrue(ssh.commands[-1][2]);self.assertNotIn('\n',command)
  self.assertIn(d.MANIFEST_SHA,command);self.assertNotIn('password',command.lower())
  online.sudo_stdin_command(command)
  ssh=FakeSSH();path=self.state/'commands.json';self.assertEqual(d.stage_apply(ssh,'cp_0022',bundle,bootstrap,path,registry=self.registry,plan=plan)['status'],'staged_not_applied')
  handoff=json.loads(path.read_text());self.assertIn('-T',handoff['dry_run_argv']);self.assertIn('-T',handoff['apply_argv'])
  self.assertFalse(any(c[2] for c in ssh.commands))

if __name__=='__main__':unittest.main()
