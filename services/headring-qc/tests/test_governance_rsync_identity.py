"""Real service SSH builder and handoff transfer; no SSH or rsync is executed."""
import importlib.util
from pathlib import Path
import shlex
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/headring-qc'))
sys.path.insert(0, str(ROOT / 'orangepi/src/orangepi_collect/common'))
import handoff_receiver
from handoff_receiver import HandoffReceiver
from host.qc.tests.governance_fixtures import sample_registry

DEVICE_IDS = ('cp_0014', 'cp_0015', 'cp_0018', 'cp_0020', 'cp_0022', 'cp_0042')
SESSION = 'L3-2026-11-13-40177'


class RsyncIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root / 'data'
        self.data.mkdir()
        # Execute the real, import-safe source in an isolated module namespace;
        # configure_runtime only receives synthetic paths and explicit config.
        spec = importlib.util.spec_from_file_location('synthetic_rsync_service',
                    ROOT / 'services/headring-qc/service.py')
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)
        self.registry = sample_registry(DEVICE_IDS)
        self.service.configure_runtime(config={'data_root': str(self.data), 'devices': [
            dict(id=did, ssh_alias='stale-alias', address='198.51.100.250') for did in DEVICE_IDS]},
            registry=self.registry, app_root=root / 'app', agent_source='synthetic agent source',
            identity_file='/synthetic/trust/id', known_hosts='/synthetic/trust/known_hosts',
            config_path='/synthetic/config.json')
        self.receiver = HandoffReceiver(self.service, registry=self.registry)
        self.receiver.store.set_control(True, 0, uuid.uuid4().hex)
        self.receiver._idle = Mock()  # No device RPC: this suite tests transport argv.
        self.process = types.SimpleNamespace(poll=lambda: 0, returncode=0)

    def job(self, did):
        details = dict(session_id=SESSION, path='/synthetic/capture/' + SESSION)
        identifier = self.receiver.store.enqueue(did, details)
        return self.receiver.store.job(identifier)

    def transfer(self, job, *, push=False, target=None):
        target = target or self.data / 'transfer' / job['device'] / SESSION
        with patch.object(handoff_receiver.subprocess, 'Popen', return_value=self.process) as popen:
            self.receiver._transfer(job, target, push=push)
        popen.assert_called_once()
        return popen.call_args.args[0]

    def test_registry_42_alias_not_canonical(self):
        command = self.transfer(self.job('cp_0042'))
        device = next(d for d in self.registry.devices if d.device_id == 'cp_0042')
        self.assertNotEqual(device.ssh_alias, device.device_id)
        self.assertTrue(command[-2].startswith(device.ssh_alias + ':'))

    def test_all_devices_pull_push_use_real_service_strict_identity(self):
        for device in self.registry.devices:
            for push in (False, True):
                with self.subTest(device=device.device_id, push=push):
                    job = self.job(device.device_id)
                    command = self.transfer(job, push=push)
                    transport = self.service.ssh(device.device_id, '')
                    rsh = shlex.split(command[command.index('-e') + 1])
                    self.assertEqual(rsh, transport[:-2])
                    self.assertIn('-oHostName=' + device.address, rsh)
                    self.assertIn('-oHostKeyAlias=' + device.host_key_alias, rsh)
                    self.assertIn('-oUser=' + device.ssh_user, rsh)
                    self.assertNotEqual(device.host_key_alias, device.ssh_alias)
                    self.assertNotEqual(device.address, '198.51.100.250')
                    self.assertEqual(rsh[rsh.index('-F') + 1], '/dev/null')
                    self.assertEqual(rsh[rsh.index('-i') + 1], '/synthetic/trust/id')
                    for option in ('-oStrictHostKeyChecking=yes', '-oBatchMode=yes',
                                   '-oUserKnownHostsFile=/synthetic/trust/known_hosts',
                                   '-oIdentitiesOnly=yes', '-oProxyCommand=none',
                                   '-oProxyJump=none', '-oControlMaster=no',
                                   '-oControlPersist=no', '-oClearAllForwardings=yes'):
                        self.assertIn(option, rsh)
                    self.assertIn('--protect-args', command)
                    remote = command[-1] if push else command[-2]
                    self.assertTrue(remote.startswith(device.ssh_alias + ':/'))
                    self.assertTrue(remote.endswith('/' + SESSION + '/'))
                    self.assertNotIn(str(self.receiver.root / 'ssh/config'), command)

    def assert_rejected_before_process(self, job, *, push=False, target=None):
        self.receiver._idle.reset_mock()
        target = target or self.data / 'rejected' / SESSION
        with patch.object(handoff_receiver.subprocess, 'Popen') as popen:
            with self.assertRaises((ValueError, TypeError)):
                self.receiver._transfer(job, target, push=push)
        popen.assert_not_called()
        self.receiver._idle.assert_not_called()

    def test_wrong_unknown_or_denied_job_rejected_before_process(self):
        good = self.job('cp_0042')
        for details in (dict(good['details'], session_id='L3-other-session'),
                        dict(good['details'], device_id='cp_0014'), {}):
            for push in (False, True):
                with self.subTest(details=details, push=push):
                    self.assert_rejected_before_process(dict(good, details=details), push=push)
        for device in ('cp_0043', 'test-cp_0042', None):
            with self.subTest(device=device):
                self.assert_rejected_before_process(dict(good, device=device))
        self.receiver.registry = sample_registry(DEVICE_IDS, receive=())
        self.assert_rejected_before_process(good)

    def test_registry_and_config_digest_drift_rejected_before_process(self):
        job = self.job('cp_0042')
        with patch.object(self.service, 'REGISTRY', sample_registry(DEVICE_IDS, receive=('cp_0042',))):
            self.assert_rejected_before_process(job)
        with patch.dict(self.receiver.config, registry_digest='0' * 64):
            self.assert_rejected_before_process(job)

    def test_missing_helper_alias_drift_or_incomplete_argv_fails_closed(self):
        job = self.job('cp_0042')
        transport = self.service.ssh(job['device'], '')
        stale_config = list(transport)
        stale_config[stale_config.index('-F') + 1] = '/synthetic/untrusted-ssh-config'
        for value in (None, [], ['ssh', transport[-2], ''],
                      stale_config, ['ssh', '-F', '/dev/null', transport[-2], ''],
                      transport[:-2] + [job['device'], ''],
                      transport[:-1] + ['unexpected remote command'],
                      transport[:5] + [''] + transport[6:]):
            with self.subTest(returned_type=type(value).__name__):
                with patch.object(self.service, 'ssh', return_value=value):
                    self.assert_rejected_before_process(job)
        with patch.object(self.service, 'ssh', None):
            self.assert_rejected_before_process(job)

    def test_malicious_paths_and_sessions_rejected_before_process(self):
        good = self.job('cp_0042')
        for path in ('relative/' + SESSION, '/synthetic/../capture/' + SESSION,
                     '/synthetic//capture/' + SESSION, '/synthetic/$(touch marker)/' + SESSION,
                     '/synthetic/capture;false/' + SESSION, '/synthetic/capture\n/' + SESSION,
                     '/synthetic/capture/other-session', '/synthetic/quote\'/' + SESSION):
            with self.subTest(path=path):
                self.assert_rejected_before_process(dict(good, details=dict(good['details'], path=path)))
        for sid in ('../escape', '40177', SESSION + ';false', SESSION + '\n'):
            with self.subTest(session=sid):
                for push in (False, True):
                    self.assert_rejected_before_process(dict(good, session=sid), push=push)
        self.assert_rejected_before_process(good, target=Path('relative') / SESSION)
        self.assert_rejected_before_process(good, target=self.data / '..' / SESSION)

    def test_pure_argv_builder_and_rsh_quoting_without_io(self):
        job = self.job('cp_0042')
        transport = self.service.ssh(job['device'], '')
        # A trusted helper's opaque argument is quoted, not re-split or executed.
        transport.insert(1, "-oSyntheticLabel=spaces ' and quotes")
        target = self.data / "local space ' quote" / SESSION
        with patch.object(self.service, 'ssh', return_value=transport), \
             patch.object(Path, 'open', side_effect=AssertionError('argv builder opened a file')), \
             patch.object(Path, 'resolve', side_effect=AssertionError('argv builder resolved a path')), \
             patch.object(handoff_receiver.subprocess, 'Popen', side_effect=AssertionError('argv builder started process')):
            command = self.receiver._transfer_argv(job, target)
        self.assertEqual(shlex.split(command[command.index('-e') + 1]), transport[:-2])
        self.assertEqual(command[-1], str(target) + '/')


if __name__ == '__main__':
    unittest.main()
