"""Local-only adapter tests: no real SSH, keys, trust or configuration."""
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from host.common.strict_json import canonical_json, strict_json_loads
from host.qc.tests.governance_fixtures import (
    sample_device, sample_probe_policy, sample_probe_result,
)
from host.qc.fleet_monitor.readonly_probe import (
    _REMOTE_SCRIPT, _admit_identity_file, _decode_result, _payload, _ssh_argv,
    _validate_request, probe_once,
)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.device = sample_device()
        self.policy = sample_probe_policy()
        self.digest = 'a' * 64

    def test_contract_fields_and_reject_foreign_envelopes(self):
        result = sample_probe_result(registry_digest=self.digest)
        line = canonical_json(asdict(result)).encode()
        self.assertEqual(_decode_result(line, self.device, self.digest, 1), result)
        for kwargs in ({'device_id': 'cp_0043'}, {'generation': 2},
                       {'registry_digest': 'b' * 64}):
            bad = replace(result, **kwargs)
            with self.assertRaises(ValueError):
                _decode_result(canonical_json(asdict(bad)).encode(), self.device, self.digest, 1)
        for bad in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.assertRaises(ValueError):
                _decode_result(bad, self.device, self.digest, 1)

    def test_request_gates(self):
        for device, digest, generation in (
                (replace(self.device, enrolled=False), self.digest, 1),
                (replace(self.device, monitor_enabled=False), self.digest, 1),
                (self.device, 'bad', 1), (self.device, self.digest, True),
                (self.device, self.digest, -1)):
            with self.assertRaises(ValueError):
                _validate_request(device, digest, generation, self.policy)

    def test_argv_binds_single_device_and_disables_ssh_side_effects(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'admitted'
            config.write_text('Host ' + self.device.ssh_alias + '\n IdentityFile /synthetic/key\n')
            policy = replace(self.policy, ssh_config=str(config))
            argv = _ssh_argv(self.device, policy)
        self.assertEqual(argv[:4], ['/usr/bin/ssh', '-F', '/dev/null', '-T'])
        for token in ('StrictHostKeyChecking=yes', 'BatchMode=yes',
                      'HostName=' + self.device.address, 'HostKeyAlias=' + self.device.host_key_alias,
                      'User=' + self.device.ssh_user, 'ControlMaster=no', 'ControlPersist=no',
                      'ProxyCommand=none', 'ProxyJump=none', 'IdentityAgent=none',
                      'PermitLocalCommand=no', 'ClearAllForwardings=yes',
                      'GlobalKnownHostsFile=/dev/null', 'KnownHostsCommand=none'):
            self.assertIn(token, argv)
        self.assertEqual(argv[-2], self.device.ssh_alias)
        self.assertNotIn('accept-new', ' '.join(argv))
        self.assertNotIn('-f', argv)

    def test_config_grammar_fail_closed(self):
        forbidden = ('Match exec x', 'Include /synthetic/extra', 'ProxyCommand x',
                     'ProxyJump x', 'LocalCommand x', 'Host *', 'ControlMaster yes',
                     'IdentityAgent x', 'HostName 192.0.2.42', 'User other',
                     'KnownHostsCommand x', 'IdentityFile /synthetic/%h',
                     'IdentityFile /synthetic/../key', 'IdentityFile ~/.ssh/id',
                     'IdentityFile /synthetic/key\n IdentityFile /synthetic/key2')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config'
            for line in forbidden:
                path.write_text('Host fixture\n ' + line + '\n')
                with self.subTest(line=line), self.assertRaises(ValueError):
                    _admit_identity_file(str(path), 'fixture')
            path.write_text('Host fixture\n IdentityFile /synthetic/key\n')
            self.assertEqual(_admit_identity_file(str(path), 'fixture'), '/synthetic/key')
            with self.assertRaises(ValueError):
                _admit_identity_file(str(path), 'other')
            link = Path(root) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                _admit_identity_file(str(link), 'fixture')

    def _remote(self, identity, *, disk=None, source=True, prefix='', source_symlink=False,
                identity_symlink=False):
        with tempfile.TemporaryDirectory() as root:
            identity_path = Path(root) / 'identity'
            identity_path.write_text(identity)
            component = Path(root) / 'component.py'
            component.write_text('raise RuntimeError("MUST NOT EXECUTE")\n')
            if source_symlink:
                link = Path(root) / 'link.py'
                link.symlink_to(component)
                component = link
            if identity_symlink:
                link = Path(root) / 'identity-link'
                link.symlink_to(identity_path)
                identity_path = link
            policy = replace(self.policy, identity_path=str(identity_path), disk_mount=disk,
                             component_paths=(('collector', str(component)),) if source else ())
            process = subprocess.run([sys.executable, '-I', '-S', '-c', prefix + _REMOTE_SCRIPT],
                                     input=_payload(self.device, self.digest, 3, policy),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7)
            results = [_decode_result(line, self.device, self.digest, 3)
                       for line in process.stdout.splitlines()]
            return results, process.returncode

    def test_fixed_remote_real_proc_and_source_hash_not_runtime(self):
        results, rc = self._remote(self.device.device_id + '\n')
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(results), 3)
        final = results[-1]
        self.assertTrue(final.identity_verified)
        fields = dict(final.fields)
        self.assertGreater(strict_json_loads(fields['ram_available_bytes'].value_json), 0)
        installed = strict_json_loads(fields['installed_components'].value_json)
        self.assertEqual(installed['collector']['status'], 'observed')
        self.assertTrue(installed['collector']['source'].startswith('file:'))
        self.assertIsNone(fields['runtime_components'].value_json)
        self.assertIsNone(fields['controller_state'].value_json)
        self.assertIsNone(fields['disk_available_bytes'].value_json)

    def test_wrong_identity_has_no_evidence(self):
        for identity in ('cp_0043', '{"device_id":"cp_0042"}', 'cp_0042\nextra'):
            results, _ = self._remote(identity)
            self.assertEqual(results[-1].status, 'identity_rejected')
            self.assertFalse(results[-1].identity_verified)
            self.assertTrue(all(e.value_json is None for _, e in results[-1].fields))

    def test_disk_hang_remote_deadline_keeps_ram_stage(self):
        # Synthetic statvfs blocks in foreground. No mounts or real disk probes.
        prefix = 'import os,time\nos.statvfs=lambda path: time.sleep(60)\n'
        results, rc = self._remote(self.device.device_id, disk='/synthetic/mount', prefix=prefix)
        self.assertEqual(rc, 124)
        self.assertIsNotNone(dict(results[-1].fields)['ram_available_bytes'].value_json)
        self.assertIsNone(dict(results[-1].fields)['disk_available_bytes'].value_json)

    def test_source_is_data_not_executed_and_symlink_rejected(self):
        # The successful source test would fail immediately if source were exec'd.
        with self.assertRaises(ValueError):
            _validate_request(self.device, self.digest, 1,
                              replace(self.policy, component_paths=(('collector', '/dev/raw.py'),)))
        results, _ = self._remote(self.device.device_id, source_symlink=True)
        installed = strict_json_loads(dict(results[-1].fields)['installed_components'].value_json)
        self.assertEqual(installed['collector']['status'], 'unknown')
        self.assertIsNone(installed['collector']['sha256'])
        results, _ = self._remote(self.device.device_id, identity_symlink=True)
        self.assertEqual(results[-1].status, 'identity_rejected')

    def test_probe_once_public_api_uses_requested_generation(self):
        from host.qc.tests.test_governance_isolation import synthetic_factory
        from host.qc.fleet_monitor import isolated_transport
        original = isolated_transport.ProbeScheduler
        def constructor(devices, **kwargs):
            scheduler = original(devices, **kwargs)
            scheduler._factory = synthetic_factory()
            return scheduler
        with patch.object(isolated_transport, 'ProbeScheduler', side_effect=constructor):
            result = probe_once(self.device, registry_digest=self.digest, generation=17, policy=self.policy)
        self.assertEqual(result.generation, 17)
        self.assertEqual(result.device_id, self.device.device_id)


if __name__ == '__main__':
    unittest.main()
