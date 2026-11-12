import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cp15_release as cp
import fleet
import online


def load_transaction():
    path=cp.RESOURCE/'transaction.py';spec=importlib.util.spec_from_file_location('cp15_transaction_fixture',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class CanaryRegistryTests(unittest.TestCase):
    def setUp(self):self.manifest,self.request,self.before,self.bundle,self.bootstrap=cp.registered()
    def live(self,after=False):
        hashes=dict(self.before)
        if after:hashes.update({x['path']:x['sha256'] for x in self.manifest['files']})
        def state(path,sha):
            parts=path.split('/')[:-1]
            parents=[{'type':'dir','path':'/'.join(parts[:i])} for i in range(1,len(parts)+1)]
            return {'type':'absent','parents':parents} if sha is None else {'type':'regular','sha256':sha,'parents':parents}
        return {'files':{p:state(p,h) for p,h in hashes.items()}}
    def test_static_bundle_and_exact_allowlist(self):
        self.assertEqual(len(self.request['files']),237);self.assertEqual(len(self.manifest['files']),15)
        self.assertEqual([x['path'] for x in self.manifest['files']],list(cp.PATHS))
        self.assertIsNone(self.before[cp.NEW])
        self.assertEqual(fleet.sha(self.bundle),cp.BUNDLE_SHA)
        self.assertEqual(fleet.sha(self.bootstrap),cp.BOOTSTRAP_SHA)
    def test_before_after_unknown_and_missing_path(self):
        self.assertEqual(cp.classify(self.live(),self.manifest,self.before),'before')
        self.assertEqual(cp.classify(self.live(True),self.manifest,self.before),'after')
        for mutate in ('mixed','new_exists','symlink','parent'):
            live=self.live()
            if mutate=='mixed':
                item=self.manifest['files'][0];live['files'][item['path']]['sha256']=item['sha256']
            elif mutate=='new_exists':live['files'][cp.NEW]={'type':'regular','sha256':'x','parents':[]}
            elif mutate=='symlink':live['files'][cp.PATHS[0]]={'type':'symlink','parents':[]}
            else:live['files'][cp.PATHS[0]]['parents']=[{'type':'symlink'}]
            with self.subTest(mutate=mutate),self.assertRaises(online.Failure):cp.classify(live,self.manifest,self.before)
    def test_only_cp15_and_no_unknown_baseline_override(self):
        for args in (['--targets','cp_0014'],['--targets','cp_0015','--approve-unknown-baseline']):
            with self.subTest(args=args),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                fleet.main(['--release',cp.RELEASE,'--offline-plan',*args])
    def test_offline_plan_is_not_deployment(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=fleet.main(['--release',cp.RELEASE,'--targets','cp_0015','--offline-plan'])
        value=json.loads(output.getvalue());self.assertEqual(code,0);self.assertFalse(value['deploy_ready'])
    def test_busy_gate_rejects_before_or_after_hash_classification(self):
        live=dict(device_id='cp_0015',project=online.PROJECT,errors=[],api={'state':'ready','busy':False,'session':None,
            'privacy':{'required':False,'phase':'idle','stop_confirmed':True,'session_id':''}},
            services={'embodied-pi-collect':'active','camd':'active'},capture_workers=[],capture_locks=[{'result':'free'}],protected={'x':{}},python_module_shadow_candidates={})
        with self.assertRaises(online.Failure):online.gates(live,'cp_0015')
    def test_apply_rejects_plan_drift_and_all_after_is_idempotent(self):
        args=SimpleNamespace(release=cp.RELEASE,targets=['cp_0015'],approve_unknown_baseline=False,audit_recordings=False,
            local_root=[],apply=True,verify=False,accepted_plan='a'*64,offline_plan=False,address=[],bind_interface=None,
            bind_address=None,ssh_config=Path('/unused'),state_dir=Path('/unused'),sudo_command_file=None)
        parser=SimpleNamespace(error=lambda message:(_ for _ in ()).throw(AssertionError(message)))
        for after in (False,True):
            live=self.live(after);live.update(protected={'fixed':'hash'},camd_pid='42')
            plan=dict(release=cp.RELEASE,device='cp_0015',manifest_sha256=cp.MANIFEST_SHA,address='192.168.20.109',
                hostkey_alias='192.168.1.119',created_at=time.time(),files=live['files'],protected=live['protected'],
                camd_pid='42' if after else 'different',profile={'policy':'advisory'},baseline='after' if after else 'before')
            output=io.StringIO()
            with patch.object(cp.online,'config_for',return_value={'hostkeyalias':'192.168.1.119'}),\
                 patch.object(cp.online,'SSH'),patch.object(cp.online,'read_live',return_value=live),\
                 patch.object(cp.online,'gates'),patch.object(cp.online,'load_plan',return_value=plan),\
                 patch.object(cp.delta_release,'read_profile',return_value={'policy':'advisory'}),\
                 patch.object(cp,'stage_apply') as stage,contextlib.redirect_stdout(output):
                result=cp.main(args,parser)
            stage.assert_not_called()
            self.assertEqual(result,0 if after else 2)
            self.assertEqual(json.loads(output.getvalue())['status'],'already_installed_verified' if after else 'offline_or_rejected')


class CanaryTransactionTests(unittest.TestCase):
    def setUp(self):self.tx=load_transaction()
    def test_root_bundle_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with zipfile.ZipFile(cp.RESOURCE/'bundle.zip') as z:z.extractall(root)
            with patch.object(self.tx,'require_root_private_tree'):
                self.assertEqual(len(self.tx.delta_bundle(root)['files']),15)
                target=root/'payload'/cp.PATHS[0];target.write_bytes(b'tampered')
                with self.assertRaises(self.tx.Abort):self.tx.delta_bundle(root)
    def test_inventory_checks_all_236_and_missing_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'orangepi/src').mkdir(parents=True)
            profile=root/'profiles/eeg/orangepi';profile.mkdir(parents=True);(profile/'src').symlink_to(root/'orangepi/src',target_is_directory=True)
            (profile/'configs').mkdir();(profile/'configs/devices.yaml').write_text('{}')
            states={p:({'type':'absent'} if h is None else {'type':'regular','sha256':h}) for p,h in self.tx.EXPECTED_BEFORE.items()}
            with patch.object(self.tx,'PROJECT',root),patch.object(self.tx,'snapshot',return_value=states),patch.object(self.tx,'check_diagnostic_paths'):
                self.assertEqual(len(self.tx.delta_inventory()),237)
                states[cp.NEW]={'type':'regular','sha256':'unexpected'}
                with self.assertRaises(self.tx.Abort):self.tx.delta_inventory()
    def test_source_parent_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'orangepi').mkdir();(root/'other').mkdir();(root/'orangepi/src').symlink_to(root/'other',target_is_directory=True)
            with patch.object(self.tx,'PROJECT',root),self.assertRaises(self.tx.Abort):self.tx.file_state(cp.PATHS[0])
    def test_diagnostics_symlinks_and_modes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runtime=root/'orangepi/runtime';runtime.mkdir(parents=True)
            outside=root/'outside';outside.mkdir();(runtime/'diagnostics').symlink_to(outside,target_is_directory=True)
            with patch.object(self.tx,'PROJECT',root):
                with self.assertRaises(self.tx.Abort):self.tx.check_diagnostic_paths()
                (runtime/'diagnostics').unlink();(runtime/'diagnostics').mkdir(mode=0o700)
                event=runtime/'diagnostics/events.jsonl';event.write_text('{}');event.chmod(0o644)
                with self.assertRaises(self.tx.Abort):self.tx.check_diagnostic_paths()
                event.chmod(0o600);self.tx.check_diagnostic_paths()
    def test_existing_and_new_file_atomic_install_and_full_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';target=project/'orangepi/src/orangepi_collect/control';target.mkdir(parents=True)
            existing=target/'dashboard.py';existing.write_bytes(b'old')
            rels=['orangepi/src/orangepi_collect/control/dashboard.py',cp.NEW]
            stage=root/'stage/payload'
            for rel in rels:
                p=stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(('new:'+rel).encode())
            import hashlib
            files=[dict(path=p,sha256=hashlib.sha256((stage/p).read_bytes()).hexdigest(),git_mode='100644') for p in rels]
            with patch.object(self.tx,'PROJECT',project),patch.object(self.tx.os,'chown'):
                before=self.tx.snapshot(rels);meta=self.tx.backup_files(files,root/'backup',before);installed=[]
                self.tx.atomic_install(files,root/'stage',meta,before,installed)
                self.assertEqual(len(installed),2);self.assertTrue((project/cp.NEW).exists())
                self.assertEqual(self.tx.rollback(installed,root/'backup',meta,{x['path']:x['sha256'] for x in files}),[])
                self.assertEqual(self.tx.verify_restored(installed,root/'backup',meta),[])
                self.assertEqual(existing.read_bytes(),b'old');self.assertFalse((project/cp.NEW).exists())


if __name__=='__main__':unittest.main()
