import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import fleet
import online

def fixture():
    path='orangepi/src/fixture.py'; data=b'new'; digest=fleet.sha(data)
    manifest={'files':[{'path':path,'sha256':digest}]}
    live={'device_id':'cp_0014','project':online.PROJECT,'observed_utc':'2026-01-01T00:00:00Z',
          'errors':[],'api':{'state':'idle','busy':False,'session':None,'privacy':{'required':False,'phase':'idle','stop_confirmed':True,'session_id':None}},
          'services':{'embodied-pi-collect':'active','camd':'active'},'capture_workers':[],
          'capture_locks':[{'path':'orangepi/runtime/capture.lock','result':'free'}], 'camd_pid':'42',
          'protected':{'/opt/egorecord/calibrations/a':{'sha256':'x'}},
          'files':{path:{'type':'regular','sha256':digest,'parents':[]}},'system_components':{}}
    return manifest,live

class FakeSSH:
    instances=[]
    def __init__(self,*args): self.calls=[]; self.__class__.instances.append(self)
    def require_tty(self): pass
    def run(self, command, data=None, interactive=False):
        self.calls.append((command,data,interactive))
        if command.startswith('umask 077; mktemp'): return b'/home/orangepi/.headring-stage-Abc12345\n'
        return b''

class OnlineTests(unittest.TestCase):
    def test_embedded_production_preflight_compiles(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('real_preflight',online.PREFLIGHT)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        compile(module.REMOTE,'preflight','exec')

    def test_component_diff_three_states(self):
        base={'sha256':'a'*64,'mode':0o644,'uid':0,'gid':0,'type':'regular'}
        live={'system_components':{'/one':base,'/two':{'type':'absent'},'/three':dict(base,sha256='b'*64)}}
        bundle={'components':{str(i):{'destination':p,'sha256':'a'*64,'mode':0o644} for i,p in enumerate(('/one','/two','/three'))}}
        self.assertEqual([x['status'] for x in online.component_diff(live,bundle)],['same','absent','different'])

    def test_address_override_preserves_strict_host_identity(self):
        with mock.patch.object(online,'config_for',return_value={'user':'orangepi','hostname':'192.168.1.103'}):
            ssh=online.SSH(Path('/fixture/config'),'cp_0014','192.0.2.10','eth0','192.0.2.2')
        self.assertIn('StrictHostKeyChecking=yes',ssh.argv)
        self.assertIn('HostKeyAlias=192.168.1.103',ssh.argv)
        self.assertIn('HostName=192.0.2.10',ssh.argv)
        self.assertIn('BindInterface=eth0',ssh.argv)

    def test_remote_read_program_executes_locally_in_fixture(self):
        manifest,_=fixture()
        class LocalSSH:
            def run(self,command):
                return subprocess.run(command,shell=True,check=True,stdout=subprocess.PIPE).stdout
        with tempfile.TemporaryDirectory() as d:
            preflight=Path(d)/'preflight.py'
            preflight.write_text("REMOTE = 'import json,sys; r=json.load(sys.stdin); print(json.dumps({\\\"project\\\":r[\\\"project\\\"]}))'\n")
            tool=Path(d)/'tool.py'; tool.write_text("def protected_snapshot(): return {'fixture': {'sha256': 'ok'}}\n")
            with mock.patch.object(online,'PREFLIGHT',preflight), mock.patch.object(online,'TOOL',tool):
                result=online.read_live(LocalSSH(),manifest)
            self.assertEqual(result['project'],online.PROJECT)
            self.assertEqual(result['protected']['fixture']['sha256'],'ok')
            self.assertEqual(len(result['system_components']),5)

    def test_wrong_identity_busy_and_missing_lock_refuse(self):
        _, live=fixture()
        for change in ({'device_id':'cp_0018'}, {'api':{'state':'recording','busy':True}}, {'capture_locks':[]}):
            x=copy.deepcopy(live); x.update(change)
            with self.assertRaises(online.Failure): online.gates(x,'cp_0014')

    def test_plan_drift_rejected(self):
        manifest,live=fixture(); bundle={'manifest_sha256':'manifest'}
        plan=online.prepare_plan(live,manifest,'cp_0014',bundle)
        changed=copy.deepcopy(live); changed['files'][next(iter(changed['files']))]['sha256']='0'*64
        with self.assertRaises(online.Failure): online.matches(plan,changed,'cp_0014',manifest,bundle)
        changed=copy.deepcopy(live); changed['protected']['/opt/egorecord/calibrations/a']['sha256']='changed'
        with self.assertRaises(online.Failure): online.matches(plan,changed,'cp_0014',manifest,bundle)

    def test_unapproved_unknown_rejected(self):
        manifest,live=fixture(); live['files'][next(iter(live['files']))]['sha256']='1'*64
        with mock.patch.object(online,'historical',return_value=False):
            with self.assertRaises(online.Failure): online.prepare_plan(live,manifest,'cp_0014',{'manifest_sha256':'m'})
            self.assertTrue(online.prepare_plan(live,manifest,'cp_0014',{'manifest_sha256':'m'},True)['unknown_approved'])

    def test_real_staging_builds_root_dryrun_then_apply_command(self):
        manifest,live=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            package=Path(tmp)/'package'; package.write_bytes(b'fixture bundle')
            usb=Path(tmp)/'usb'; usb.write_bytes(b'fixture usb')
            bundle={'manifest_sha256':'a'*64,'bundle':str(package),'bundle_sha256':fleet.sha(package.read_bytes()),
                    'component_bundle':str(usb),'component_bundle_sha256':fleet.sha(usb.read_bytes()),'component_manifest_sha256':'b'*64}
            plan=online.prepare_plan(live,manifest,'cp_0014',bundle)
            ssh=FakeSSH(); root=online.stage_and_apply(ssh,live,plan,bundle,'cp_0014')
            self.assertTrue(root.startswith('/var/backups/headring-software-input/cp_0014-'))
            cmd,data,interactive=ssh.calls[-1]
            self.assertTrue(interactive); self.assertIsNone(data)
            self.assertIn('sha256sum -c',cmd); self.assertIn('sudo sh -c',cmd)
            self.assertEqual(cmd.count('--device cp_0014'),5)
            self.assertEqual(cmd.count('--apply'),2)
            self.assertIn('remote_components.py',cmd); self.assertIn('--verify',cmd)

    def test_staged_command_file_separates_dryrun_from_apply(self):
        manifest,live=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            pkg=Path(tmp)/'pkg'; pkg.write_bytes(b'code')
            usb=Path(tmp)/'usb'; usb.write_bytes(b'usb')
            bundle={'manifest_sha256':'a'*64,'bundle':str(pkg),'bundle_sha256':fleet.sha(pkg.read_bytes()),
                    'component_bundle':str(usb),'component_bundle_sha256':fleet.sha(usb.read_bytes()),'component_manifest_sha256':'b'*64}
            plan=online.prepare_plan(live,manifest,'cp_0014',bundle)
            ssh=FakeSSH(); ssh.argv=['ssh','-F','fixture','cp_0014']; target=Path(tmp)/'commands.json'
            result=online.stage_and_apply(ssh,live,plan,bundle,'cp_0014',target)
            commands=json.loads(target.read_text())
            self.assertEqual(result['status'],'staged_not_applied')
            self.assertNotIn('--apply',commands['dry_run_argv'][-1])
            self.assertEqual(commands['apply_verify_argv'][-1].count('--apply'),2)
            self.assertIn('-T',commands['dry_run_argv']); self.assertNotIn('-tt',commands['dry_run_argv'])
            self.assertTrue(commands['dry_run_argv'][-1].startswith('sudo -S -p '+online.SUDO_PROMPT+' '))
            self.assertIn('local PTY credential helper',commands['warning'])
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            self.assertFalse(any(call[2] for call in ssh.calls))

    def test_cli_fake_transport_plan_apply_and_verify(self):
        manifest,live=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp); pkg=state/'pkg'; pkg.write_bytes(b'fixture')
            bundle={'manifest_sha256':'m','bundle':str(pkg),'bundle_sha256':fleet.sha(pkg.read_bytes())}
            def build(_): return dict(bundle)
            def read(_ssh,_manifest): return copy.deepcopy(live)
            with mock.patch.object(fleet,'build',side_effect=build), mock.patch.object(fleet.json,'loads',return_value=manifest), \
                 mock.patch.object(online,'SSH',FakeSSH), mock.patch.object(online,'read_live',side_effect=read), \
                 mock.patch.object(online,'stage_and_apply',return_value='/var/backups/example') as apply, \
                 mock.patch.object(online,'stage_components_verify',return_value='/var/backups/verify'), \
                 mock.patch.object(online,'load_plan') as load:
                # Avoid patching global json.loads when reading stored plan by constructing it directly.
                load.return_value=online.prepare_plan(live,manifest,'cp_0014',bundle)
                out=io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc=fleet.main(['--targets','cp0014','--address','cp0014=192.0.2.4','--state-dir',tmp])
                self.assertEqual(rc,0); self.assertIn('plan_ready',out.getvalue()); apply.assert_not_called()
                out=io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc=fleet.main(['--targets','cp0014','--state-dir',tmp,'--apply','--accepted-plan','a'*64])
                self.assertEqual(rc,3); self.assertIn('code_verified',out.getvalue()); self.assertIn('needs_system_components',out.getvalue())
                apply.assert_called_once()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc=fleet.main(['--targets','cp0014','--state-dir',tmp,'--verify','--accepted-plan','a'*64])
                self.assertEqual(rc,3)

    def test_verify_rejects_bad_installed_hash(self):
        manifest,live=fixture(); plan=online.prepare_plan(live,manifest,'cp_0014',{'manifest_sha256':'m'})
        live['files'][next(iter(live['files']))]['sha256']='1'*64
        with self.assertRaises(online.Failure): online.verify(live,plan,manifest,'cp_0014')

    def test_verified_code_and_five_components_report_ready(self):
        manifest,live=fixture()
        items={}; states={}
        for i in range(5):
            dest='/fixture/'+str(i); digest=('%x'%i)*64
            items[str(i)]={'destination':dest,'sha256':digest,'mode':0o644}
            states[dest]={'type':'regular','sha256':digest,'mode':0o644,'uid':0,'gid':0}
        live['system_components']=states; live['component_timer']={'is-enabled':'enabled','is-active':'active'}
        bundle={'manifest_sha256':'m','components':items}
        plan=online.prepare_plan(live,manifest,'cp_0014',bundle)
        self.assertTrue(online.verify(live,plan,manifest,'cp_0014',bundle,True)['deploy_ready'])
        states['/fixture/0']['sha256']='f'*64
        self.assertFalse(online.verify(live,plan,manifest,'cp_0014',bundle,True)['deploy_ready'])

    def test_fake_ssh_cli_code_components_verify_vertical(self):
        manifest,live=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); pkg=root/'pkg'; pkg.write_bytes(b'code'); usb=root/'usb'; usb.write_bytes(b'usb')
            components={}; states={}
            for i in range(5):
                dest='/fixture/'+str(i); digest=('%x'%i)*64
                components[str(i)]={'destination':dest,'sha256':digest,'mode':0o644}
                states[dest]={'type':'regular','sha256':digest,'mode':0o644,'uid':0,'gid':0}
            live['system_components']=states; live['component_timer']={'is-enabled':'enabled','is-active':'active'}
            bundle={'manifest_sha256':'m','bundle':str(pkg),'bundle_sha256':fleet.sha(pkg.read_bytes()),
                    'component_bundle':str(usb),'component_bundle_sha256':fleet.sha(usb.read_bytes()),
                    'component_manifest_sha256':'b'*64,'components':components}
            plan=online.prepare_plan(live,manifest,'cp_0014',bundle)
            with mock.patch.object(fleet,'build',return_value=bundle), mock.patch.object(fleet.json,'loads',return_value=manifest), \
                 mock.patch.object(online,'SSH',FakeSSH), mock.patch.object(online,'read_live',side_effect=lambda *_:copy.deepcopy(live)), \
                 mock.patch.object(online,'load_plan',return_value=plan), contextlib.redirect_stdout(io.StringIO()) as out:
                rc=fleet.main(['--targets','cp0014','--state-dir',tmp,'--apply','--accepted-plan','a'*64])
            self.assertEqual(rc,0); self.assertIn('all_ready',out.getvalue())
            calls=FakeSSH.instances[-1].calls; commands=[x[0] for x in calls if x[2]]
            self.assertEqual(len(commands),1); self.assertIn('remote_transaction.py',commands[0]); self.assertIn('remote_components.py',commands[0]); self.assertIn('--verify',commands[0])
            damaged=dict(bundle,component_bundle_sha256='0'*64)
            with self.assertRaises(online.Failure): online.matches(plan,live,'cp_0014',manifest,damaged)

if __name__=='__main__': unittest.main()
