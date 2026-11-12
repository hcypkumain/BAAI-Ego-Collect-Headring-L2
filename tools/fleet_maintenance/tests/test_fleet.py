import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import contextlib
import io
import sys
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
fleet = load('fleet', BASE/'fleet.py')
files = load('component_files', BASE/'component_files.py')
transaction = load('transaction', BASE/'resources/remote_transaction.py')

class FleetTests(unittest.TestCase):
    def test_target_and_address_validation(self):
        self.assertEqual(fleet.target('cp_0014'), 'cp_0014')
        self.assertEqual(fleet.address('cp_0018=192.0.2.9'), ('cp_0018', '192.0.2.9'))
        for value in ('cp0014', 'cp001', 'cp0014;id', '../cp0014'):
            with self.assertRaises(Exception): fleet.target(value)

    def test_identity_and_busy_gates(self):
        with tempfile.TemporaryDirectory() as d:
            device = Path(d)/'DEVICE_ID'; device.write_text('cp_0018')
            with mock.patch.object(transaction, 'DEVICE_ID', device), mock.patch.object(transaction, 'TARGET_DEVICE', 'cp_0014'):
                with self.assertRaises(transaction.Abort): transaction.require_device()
        with self.assertRaises(transaction.Abort): transaction.assert_idle({'state':'recording','busy':True})
        with self.assertRaises(transaction.Abort): transaction.assert_idle({})

    def test_exclusions(self):
        for name in ('orangepi/src/orangepi_collect/control/reference_credentials.py', 'orangepi/src/../configs/x.py', 'orangepi/src/runtime/x.py'):
            self.assertFalse(transaction.valid_rel(name))

    def test_real_component_apply_idempotence_umask(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); old=os.umask(0o077)
            try:
                entries={'usr/local/lib/tool/helper': (b'helper',0o755), 'etc/rules': (b'rules',0o644)}
                result=files.install(root, entries, owner=os.getuid(), group=os.getgid())
                self.assertEqual(result['installed'], 2)
                self.assertEqual((root/'usr/local/lib/tool').stat().st_mode & 0o777, 0o755)
                st=(root/'usr/local/lib/tool/helper').stat()
                self.assertEqual(files.install(root, entries, owner=os.getuid(), group=os.getgid())['status'], 'already_current')
                self.assertEqual((root/'usr/local/lib/tool/helper').stat().st_ino,st.st_ino)
                with self.assertRaises(ValueError): files.install(root, {'etc/rules':(b'different',0o644)}, owner=os.getuid(),group=os.getgid())
                self.assertEqual((root/'etc/rules').read_bytes(), b'rules')
            finally: os.umask(old)

    def test_real_failure_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(RuntimeError):
                files.install(root, {'a/one':(b'one',0o644),'a/two':(b'two',0o644)}, fail_after=1, owner=os.getuid(), group=os.getgid())
            self.assertFalse((root/'a/one').exists())
            self.assertFalse((root/'a/two').exists())

    def test_reject_untrusted_write_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); sub=root/'bad'; sub.mkdir(); sub.chmod(0o777)
            with self.assertRaises(ValueError): files.install(root, {'bad/x':(b'x',0o644)}, owner=os.getuid(), group=os.getgid())
            sub.chmod(0o700); (root/'link').symlink_to(sub, target_is_directory=True)
            with self.assertRaises(ValueError): files.install(root, {'link/x':(b'x',0o644)}, owner=os.getuid(), group=os.getgid())

    def test_real_code_transaction_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); project=root/'project'; stage=root/'stage'
            paths=['orangepi/src/one.py','orangepi/src/two.py']; items=[]
            for i,rel in enumerate(paths):
                dest=project/rel; src=stage/'payload'/rel
                dest.parent.mkdir(parents=True,exist_ok=True); src.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(('old%d'%i).encode()); data=('new%d'%i).encode(); src.write_bytes(data)
                items.append({'path':rel,'sha256':fleet.sha(data),'git_mode':'100644'})
            with mock.patch.object(transaction,'PROJECT',project):
                baseline=transaction.snapshot(paths); meta=transaction.backup_files(items,root/'backup',baseline); installed=[]
                transaction.atomic_install(items,stage,meta,baseline,installed)
                self.assertEqual([(project/p).read_bytes() for p in paths],[b'new0',b'new1'])
                self.assertEqual(transaction.rollback(installed,root/'backup',meta,{i['path']:i['sha256'] for i in items}),[])
                self.assertEqual(transaction.verify_restored(installed,root/'backup',meta),[])
                self.assertEqual([(project/p).read_bytes() for p in paths],[b'old0',b'old1'])

    def test_code_already_current_does_not_stop_or_write(self):
        manifest={'commit':fleet.PIN,'files':[{'path':'orangepi/src/a.py','sha256':'hash'}]}
        report={'files':{'orangepi/src/a.py':{'type':'regular','sha256':'hash'}},'protected':{'x':1},'camd_pid':'42'}
        argv=['transaction','--bundle-dir','/bundle','--preflight','/preflight','--baseline','/baseline',
              '--device','cp_0014','--manifest-sha256','hash','--baseline-sha256','hash',
              '--preflight-sha256','hash','--apply']
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys,'argv',argv))
            for name,value in {'require_device':None,'require_root_private_tree':None,'require_root_private_tree':None,
                               'assert_idle':None}.items():
                stack.enter_context(mock.patch.object(transaction,name,return_value=value))
            stack.enter_context(mock.patch.object(transaction.os,'geteuid',return_value=0))
            stack.enter_context(mock.patch.object(transaction,'load_bundle',return_value=manifest))
            stack.enter_context(mock.patch.object(transaction.hashlib,'sha256'))
            transaction.hashlib.sha256.return_value.hexdigest.return_value='hash'
            stack.enter_context(mock.patch.object(transaction,'digest',return_value='hash'))
            stack.enter_context(mock.patch.object(Path,'read_bytes',return_value=b'fixture'))
            stack.enter_context(mock.patch.object(transaction,'compare_preflight',return_value=report))
            stack.enter_context(mock.patch.object(transaction,'api_state',return_value={}))
            stack.enter_context(mock.patch.object(transaction,'service_active',return_value=True))
            stack.enter_context(mock.patch.object(transaction,'check_locks',return_value=['lock']))
            stack.enter_context(mock.patch.object(transaction,'camd_pid',return_value='42'))
            stack.enter_context(mock.patch.object(transaction,'protected_snapshot',return_value={'x':1}))
            run=stack.enter_context(mock.patch.object(transaction,'run'))
            with contextlib.redirect_stdout(io.StringIO()) as out: transaction.main()
            self.assertIn('already_current',out.getvalue()); run.assert_not_called()

    def test_reviewed_replacement_rolls_back_and_preserves_external_edit(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'root'; root.mkdir(); p=root/'etc/a'; p.parent.mkdir(); p.write_bytes(b'old')
            entries={'etc/a':(b'new',0o644),'etc/b':(b'new-b',0o644)}
            baseline={name:files.snapshot(root/name) for name in entries}
            def fail(rel,path):
                if str(rel)=='etc/a': raise RuntimeError('injected visudo failure')
            with self.assertRaisesRegex(RuntimeError,'visudo'):
                files.install_reviewed(root,entries,baseline,Path(d)/'backup',owner=os.getuid(),group=os.getgid(),after_install=fail)
            self.assertEqual(p.read_bytes(),b'old'); self.assertFalse((root/'etc/b').exists())
            def external(rel,path):
                path.write_bytes(b'new user edit'); raise RuntimeError('injected')
            with self.assertRaisesRegex(RuntimeError,'rollback incomplete'):
                files.install_reviewed(root,entries,baseline,Path(d)/'backup2',owner=os.getuid(),group=os.getgid(),after_install=external)
            self.assertEqual(p.read_bytes(),b'new user edit')

if __name__ == '__main__': unittest.main()
