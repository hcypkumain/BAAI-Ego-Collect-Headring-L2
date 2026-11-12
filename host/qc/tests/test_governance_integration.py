"""Premerge main contracts, NOT complete G2. Real A/B/C counterexamples remain required.

Run these now; there are deliberately no skip-based claims of pipeline/probe readiness.
"""
import contextlib
import copy
import gzip
import hashlib
import io
import importlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import tarfile
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from host.common.strict_json import canonical_json, strict_json_loads
from host.common.device_registry import select_devices,upload_allowed
from host.common.governance_contract import COMPONENT_IDS
from host.qc.fleet_monitor import governance_cli as cli
from host.qc.tests.governance_fixtures import (sample_evidence, sample_probe_policy,
                                              sample_probe_result, sample_registry)
from tools import build_governance_release as builder
from tools import check_device_generality as lint

ROOT = Path(__file__).resolve().parents[3]


def synthetic_resource_digest(resources):
    """Test double of B's documented digest format, not a production fallback."""
    return hashlib.sha256(canonical_json({name: hashlib.sha256(data).hexdigest()
                                         for name, data in resources.items()}).encode()).hexdigest()


def synthetic_comparison(bom=None, observation=None):
    return {'components': [{'component_id': name, 'source': {'sha256': 'a' * 64, 'status': 'verified_files'},
                             'installed': {'sha256': None, 'status': 'unknown'},
                             'runtime': {'sha256': None, 'status': 'unknown'}} for name in COMPONENT_IDS]}


def registry_wire(registry):
    return strict_json_loads(canonical_json({'schema_version': 1,
                                            'devices': [asdict(d) for d in registry.devices]}))


class FakeScheduler:
    """Explicit local unit test double; never claims SSH/process cancellation coverage."""
    def __init__(self, devices, *, registry_digest, policy, concurrency):
        self.devices, self.digest = devices, registry_digest
        self.closed = False
        self.pending = True

    def tick(self, now_monotonic):
        pass

    def drain(self):
        if not self.pending:
            return ()
        self.pending = False
        return tuple(sample_probe_result(d.device_id, registry_digest=self.digest,
                                         fields={'ram_available_bytes': sample_evidence(1024,
                                             source='proc:meminfo', reason='private-marker')})
                     for d in self.devices)

    def close(self, timeout_s=2.0):
        self.closed = True
        return True

    def stats(self):
        return {'active': 0, 'quarantined': 0, 'workers': 0, 'children': 0,
                'queued': len(self.devices) if self.pending else 0, 'bytes': 0}


class CliPremergeTests(unittest.TestCase):
    def test_real_B_verified_files_and_unknown_or_malformed_hash(self):
        comparison = synthetic_comparison()
        output = cli._render_comparison(comparison)
        for row in output.values():
            self.assertEqual(row['source'], {'sha256': 'a' * 64, 'status': 'verified_files',
                                            'scope': 'selected-file-set'})
            for phase in ('installed', 'runtime'):
                self.assertIsNone(row[phase]['sha256'])
                self.assertEqual(row[phase]['status'], 'unknown')
        for status, sha in (('unknown', None), ('unknown', 'a' * 64),
                            ('verified_files', 'bad'), ('verified_files', True),
                            ('verified_files', 'A' * 64)):
            comparison['components'][0]['source'] = {'status': status, 'sha256': sha}
            row = cli._render_comparison(comparison)['collector']['source']
            self.assertEqual((row['sha256'], row['status']), (None, 'unknown'))

    def setUp(self):
        self.unclosed_patch = patch.object(cli, '_UNCLOSED', [])
        self.unclosed_patch.start()
        self.addCleanup(self.unclosed_patch.stop)
        self.registry = sample_registry()
        self.args = ['--registry', '/synthetic/registry.json', '--targets', 'cp_0042',
                     '--policy', '/synthetic/policy.json', '--once', '--format', 'json']

    def test_help_and_missing_targets_zero_io(self):
        for args, code in ((['--help'], 0), ([], 2),
                           (['--registry', '/synthetic/no-file', '--once'], 2)):
            with (patch.object(cli, '_read_json') as read, patch.object(cli, '_adapters') as adapters,
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
                with self.assertRaises(SystemExit) as exit:
                    cli.main(args)
                self.assertEqual(exit.exception.code, code)
                read.assert_not_called()
                adapters.assert_not_called()

    def test_invalid_duplicate_targets_before_reads(self):
        for target in ('', 'cp_0042,cp_0042', 'cp_0042,', ' cp_0042', 'cp_42'):
            args = self.args.copy()
            args[3] = target
            with patch.object(cli, '_read_json') as read, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main(args)
                read.assert_not_called()

    def test_unknown_disabled_targets_zero_adapter_and_process(self):
        for registry, target in ((self.registry, 'cp_0043'),
                                  (sample_registry(monitor=()), 'cp_0042'),
                                  (sample_registry(enrolled=()), 'cp_0042')):
            args = self.args.copy()
            args[3] = target
            with (patch.object(cli, '_read_json', return_value=registry_wire(registry)) as read,
                  patch.object(cli, '_adapters') as adapters, patch.object(subprocess, 'Popen') as popen,
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertEqual(cli.main(args), 2)
                self.assertEqual(read.call_count, 1)
                adapters.assert_not_called()
                popen.assert_not_called()

    def test_strict_json_duplicate_and_bound(self):
        with tempfile.TemporaryDirectory(prefix='governance-cli-') as directory:
            path = Path(directory) / 'synthetic.json'
            for text in ('{"schema_version":1,"schema_version":1,"devices":[]}',
                         '{"x":NaN}', ' ' * 1048577):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    cli._read_json(path)
            linked = Path(directory) / 'synthetic-link.json'
            linked.symlink_to(path)
            with self.assertRaises(OSError):
                cli._read_json(linked)
            fifo = Path(directory) / 'synthetic-fifo.json'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                cli._read_json(fifo)

    def test_missing_adapters_clear_error_not_ready(self):
        with (patch.object(cli, '_read_json', side_effect=[registry_wire(self.registry),
                                                         json.loads(canonical_json(asdict(sample_probe_policy())))]),
              patch.object(cli, '_adapters', side_effect=cli.CliError('ADAPTER_NOT_INTEGRATED')),
              contextlib.redirect_stderr(io.StringIO()) as output):
            self.assertEqual(cli.main(self.args), 3)
        self.assertEqual(json.loads(output.getvalue()), {'error': 'ADAPTER_NOT_INTEGRATED'})

    def test_six_device_isolation_unknown_runtime_no_secret_echo(self):
        compare = Mock(return_value={**synthetic_comparison(), 'private': 'private-marker'})
        with (patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')),
              patch.object(subprocess, 'Popen', side_effect=AssertionError('process forbidden'))):
            result = cli.observe(self.registry.devices, registry_digest=self.registry.digest,
                                 policy=sample_probe_policy(), scheduler_factory=FakeScheduler,
                                 compare_components=compare, bom={'synthetic': True})
        self.assertEqual(len(result['observations']), 6)
        self.assertEqual(compare.call_count, 6)
        self.assertNotIn('private-marker', canonical_json(result))
        self.assertEqual(len({(r['device_id'], r['generation']) for r in result['observations']}), 6)
        for row in result['observations']:
            self.assertIsNone(row['fields']['runtime_components']['value'])
            self.assertIsNone(row['fields']['controller_state']['value'])
            self.assertIsNone(row['fields']['ram_available_bytes']['age_seconds'])
            self.assertEqual(row['fields']['ram_available_bytes']['freshness'], 'unknown')
            self.assertIsNone(row['component_comparison']['monitor']['runtime']['sha256'])
            self.assertEqual(row['component_comparison']['monitor']['source']['scope'], 'selected-file-set')

    def test_wrong_digest_device_generation_rejected(self):
        registry = sample_registry(device_ids=('cp_0042',))
        good = sample_probe_result(registry_digest=registry.digest)

        class Mixed(FakeScheduler):
            def drain(self):
                self.pending = False
                return (replace(good, registry_digest='a' * 64), replace(good, device_id='cp_0043'),
                        replace(good, generation=2), good, good)

        result = cli.observe(registry.devices, registry_digest=registry.digest, policy=sample_probe_policy(),
                             scheduler_factory=Mixed, compare_components=synthetic_comparison, bom={})
        self.assertEqual(result['rejected_results'], 4)
        self.assertEqual(result['observations'][0]['device_id'], 'cp_0042')

    def test_staged_ram_not_drained_until_domain_finishes(self):
        created = []

        class Staged(FakeScheduler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.ticks = self.drains = 0
                created.append(self)

            def tick(self, now_monotonic):
                self.ticks += 1

            def stats(self):
                return {**super().stats(), 'active': 1 if self.ticks < 2 else 0,
                        'quarantined': 1 if self.ticks >= 2 else 0}

            def drain(self):
                self.drains += 1
                self.assert_terminal = self.ticks >= 2
                return super().drain()

        with patch.object(cli.time, 'sleep'):
            result = cli.observe(self.registry.devices, registry_digest=self.registry.digest,
                                 policy=sample_probe_policy(), scheduler_factory=Staged,
                                 compare_components=synthetic_comparison, bom={})
        self.assertEqual((created[0].ticks, created[0].drains), (2, 1))
        self.assertTrue(created[0].assert_terminal)
        self.assertEqual(result['observations'][0]['fields']['ram_available_bytes']['value'], 1024)
        self.assertIn('DEVICE_QUARANTINED', result['warning_codes'])

    def test_partial_observation_has_warnings_and_nonzero_exit(self):
        with (patch.object(cli, '_read_json', side_effect=[registry_wire(self.registry),
                json.loads(canonical_json(asdict(sample_probe_policy())))]),
              patch.object(cli, '_adapters', return_value=(FakeScheduler, synthetic_comparison)),
              patch.object(cli, '_packaged_metadata', return_value=({'synthetic': True},())),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(cli.main(self.args), 5)
        self.assertIn('PARTIAL_EVIDENCE', json.loads(output.getvalue())['warning_codes'])

    def test_interrupt_close_and_unreaped_close(self):
        created = []

        class Interrupted(FakeScheduler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def tick(self, now_monotonic):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            cli.observe(self.registry.devices, registry_digest=self.registry.digest,
                        policy=sample_probe_policy(), scheduler_factory=Interrupted,
                        compare_components=lambda b, r: {}, bom={})
        self.assertTrue(created[0].closed)

        class Unreaped(FakeScheduler):
            def close(self, timeout_s=2.0):
                return False

        with self.assertRaisesRegex(cli.CliError, '^CLOSE_NOT_REAPED$'):
            cli.observe(self.registry.devices, registry_digest=self.registry.digest,
                        policy=sample_probe_policy(), scheduler_factory=Unreaped,
                        compare_components=lambda b, r: {}, bom={})
        self.assertEqual(len(cli._UNCLOSED), 1)
        with self.assertRaisesRegex(cli.CliError, '^PRIOR_CLOSE_UNREAPED$'):
            cli.observe(self.registry.devices, registry_digest=self.registry.digest,
                        policy=sample_probe_policy(), scheduler_factory=FakeScheduler,
                        compare_components=synthetic_comparison, bom={})

    def test_empty_selection_zero_scheduler(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            cli.observe((), registry_digest=self.registry.digest, policy=sample_probe_policy(),
                        scheduler_factory=factory, compare_components=lambda b, r: {}, bom={})
        factory.assert_not_called()


class MainAIntegrationTests(unittest.TestCase):
    """Actual merged A implementations; no production service/SSH/Queue construction."""
    def setUp(self):
        self.service = importlib.import_module('services.headring-qc.service')
        self.addCleanup(importlib.reload, self.service)
        self.temp = tempfile.TemporaryDirectory(prefix='governance-A-main-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = sample_registry()

    def configure(self, registry=None, config=None):
        self.service.configure_runtime(config=config or {'data_root': str(self.root / 'data'), 'devices': []},
            registry=self.registry if registry is None else registry, app_root=self.root,
            agent_source='# synthetic agent source, never executed', identity_file='/synthetic/software-key',
            known_hosts='/synthetic/known_hosts', config_path=self.root / 'explicit.json')

    def test_service_import_and_configuration_do_not_open_runtime(self):
        with (patch('builtins.open', side_effect=AssertionError('runtime open')),
              patch.object(Path, 'read_text', side_effect=AssertionError('runtime read')),
              patch.object(Path, 'read_bytes', side_effect=AssertionError('runtime read')),
              patch.object(sqlite3, 'connect', side_effect=AssertionError('DB opened')),
              patch.object(subprocess, 'Popen', side_effect=AssertionError('process launched')),
              patch.object(threading.Thread, 'start', side_effect=AssertionError('thread started')),
              patch.object(socket, 'socket', side_effect=AssertionError('network'))):
            importlib.reload(self.service)
            self.configure()
        self.assertIs(self.service.REGISTRY, self.registry)

    def test_service_help_and_missing_registry_zero_reads_or_starts(self):
        for args, code in ((['--help'], 0), ([], 2)):
            with (patch.object(Path, 'read_text') as read, patch.object(subprocess, 'Popen') as process,
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
                with self.assertRaises(SystemExit) as error:
                    self.service.main(args)
                self.assertEqual(error.exception.code, code)
                read.assert_not_called()
                process.assert_not_called()

    def test_only_registry_change_adds42_to_actual_handoff_discovery(self):
        before = sample_registry(tuple(d.device_id for d in self.registry.devices if d.device_id != 'cp_0042'))
        config = {'data_root': str(self.root / 'data'), 'devices': [], 'minimum_capture_date': '2026-11-01'}
        self.assertNotIn('cp_0042', {d['id'] for d in self.service.transport_config(config, before)['devices']})
        self.configure(config=config)
        with patch.object(sys, 'path', [str(ROOT / 'services/headring-qc'),
                     str(ROOT / 'orangepi/src/orangepi_collect/common'), *sys.path]):
            receiver_class = importlib.import_module('services.headring-qc.handoff_receiver').HandoffReceiver
            receiver = receiver_class(self.service, registry=self.registry)
        self.assertIs(receiver.registry, self.registry)
        self.assertIs(receiver.store.registry, self.registry)
        self.assertIn('cp_0042', receiver.devices)
        receiver.store.set_control(True, 0, uuid.uuid4().hex)
        session = 'L3-2026-11-13-40177'
        calls = []
        def synthetic_rpc(device, action, **kwargs):
            calls.append((device, action))
            self.assertEqual(action, 'scan')
            return {'device_id': device, 'state': 'idle', 'items': [
                {'session_id': session, 'day': '2026-11-13', 'native_sha256': 'a' * 64,
                 'path': '/synthetic/capture/' + session, 'bytes': 1}]}
        receiver.rpc = synthetic_rpc  # Transport-only stand-in, never authentication/receipt verification.
        receiver._scan_one(receiver.devices['cp_0042'])
        jobs = receiver.store.jobs()
        self.assertEqual([(row['device'], row['session']) for row in jobs], [('cp_0042', session)])
        self.assertEqual(calls, [('cp_0042', 'scan')])
        self.assertEqual(select_devices(self.registry, ['cp_0042'], purpose='monitor')[0].device_id, 'cp_0042')
        self.assertEqual(self.service.CONFIG['registry_digest'], self.registry.digest)
        self.assertFalse(upload_allowed(self.registry, 'cp_0042', destination='EGOL2',
                                       now_utc=datetime(2026, 11, 13, tzinfo=timezone.utc)))

    def test_transport_identity_comes_only_from_frozen_registry(self):
        config = {'data_root': str(self.root / 'data'), 'devices': [
            {'id': 'cp_0042', 'address': '203.0.113.99', 'ssh_alias': 'untrusted-old-alias',
             'enabled': False, 'minimum_stop_time': 123}, {'id': 'cp_0043'}]}
        self.configure(config=config)
        self.assertNotIn('cp_0043', self.service.DEVICES)
        self.assertEqual(self.service.DEVICES['cp_0042']['minimum_stop_time'], 123)
        device = select_devices(self.registry, ['cp_0042'], purpose='receive')[0]
        self.service.DEVICES['cp_0042']['address'] = '203.0.113.98'
        argv = self.service.ssh('cp_0042', 'synthetic-command')
        self.assertEqual(argv[-2:], [device.ssh_alias, 'synthetic-command'])
        self.assertIn('-oHostName=' + device.address, argv)
        self.assertIn('-oHostKeyAlias=' + device.host_key_alias, argv)
        self.assertIn('-oUser=' + device.ssh_user, argv)
        self.assertIn('-oStrictHostKeyChecking=yes', argv)
        self.assertEqual(argv[argv.index('-F') + 1], '/dev/null')
        with self.assertRaises(ValueError):
            self.service.ssh('cp_0043', 'synthetic')
        self.configure(registry=sample_registry(receive=()))
        with self.assertRaises(ValueError):
            self.service.ssh('cp_0042', 'synthetic')

    def test_real_signed_standardizer42_uses_same_registry_and_rejects_bad_signature(self):
        from host.standardizer.tests.test_registry_adapter import context_at,signed_archive
        from host.standardizer import worker
        context = context_at(self.root, registry=self.registry)
        path, source, _ = signed_archive(context)
        self.assertIs(context.registry, self.registry)
        self.assertEqual(worker.verify_receipt(path, context=context)[1], source)
        value = strict_json_loads(path.read_text())
        value['signature'] = '0' * 64
        path.write_text(canonical_json(value))
        with self.assertRaises(ValueError):
            worker.verify_receipt(path, context=context)

    def test_service_strict_json_entry_rejects_duplicate_before_agent_or_store(self):
        registry_path = self.root / 'registry.json'
        registry_path.write_text('{"schema_version":1,"schema_version":1,"devices":[]}')
        with (patch.object(sqlite3, 'connect') as db, patch.object(subprocess, 'Popen') as process):
            with self.assertRaises(ValueError):
                self.service.main(['--registry', str(registry_path), '--config', '/synthetic/config',
                                   '--identity-file', '/synthetic/key', '--known-hosts', '/synthetic/trust'])
            db.assert_not_called()
            process.assert_not_called()


class RealG2Tests(unittest.TestCase):
    """Actual A/B/C modules and real local child domains, never real SSH/device I/O."""
    def setUp(self):
        from host.qc.fleet_monitor import isolated_transport as transport
        self.transport=transport
        self.quarantine=patch.dict(transport._PROCESS_QUARANTINE,clear=True)
        self.quarantine.start();self.addCleanup(self.quarantine.stop)
        self.retention=patch.object(cli,'_UNCLOSED',[])
        self.retention.start();self.addCleanup(self.retention.stop)
        self.temp=tempfile.TemporaryDirectory(prefix='governance-real-G2-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.registry=sample_registry();self.schedulers=[]
        self.addCleanup(self.close_all)
        self.admission=self.root/'admission'
        self.admission.write_text(''.join('Host '+d.ssh_alias+'\n IdentityFile /synthetic/unopened-software-key\n'
                                           for d in self.registry.devices))

    def close_all(self):
        for scheduler in self.schedulers:self.assertTrue(scheduler.close())
        self.assertEqual(self.transport._PROCESS_DOMAINS,{})

    def bom(self,*,path='synthetic/collector.py',data=b'# synthetic ordinary source\n'):
        from tools.fleet_maintenance.component_bom import build_bom
        records=[{'component_id':name,'files':[],'approval':{'status':'unknown','scope':[name],
                   'reason':'not reviewed','expiry':None}} for name in COMPONENT_IDS]
        records[0]['files']=[{'ref':'a'*40,'path':path,'resource':path,
                             'sha256':hashlib.sha256(data).hexdigest(),'provenance':'synthetic/file'}]
        return build_bom(source_manifest={'schema_version':1,'base_commit':'a'*40,'components':records},
                         read_blob=lambda ref,name:data,overlays=())

    def factory(self,device,digest,generation,policy):
        from host.qc.fleet_monitor.readonly_probe import _ssh_argv,_REMOTE_SCRIPT
        _ssh_argv(device,policy)  # Actual minimal admission and fixed SSH argv, no SSH execution.
        return subprocess.Popen([sys.executable,'-I','-S','-c',_REMOTE_SCRIPT],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            start_new_session=True,close_fds=True)

    def scheduler_factory(self,factory=None):
        def make(devices,*,registry_digest,policy,concurrency):
            scheduler=self.transport._test_scheduler(tuple(devices),registry_digest=registry_digest,
                policy=policy,concurrency=concurrency,factory=factory or self.factory)
            self.schedulers.append(scheduler);return scheduler
        return make

    def test_actual_reader_scheduler_B_compare_and_CLI_file_binding(self):
        from tools.fleet_maintenance.component_bom import compare_components
        device,=select_devices(self.registry,['cp_0042'],purpose='monitor')
        identity=self.root/'identity';identity.write_text(device.device_id+'\n')
        source=self.root/'collector.py';data=b'# ordinary synthetic file, never executed\n';source.write_bytes(data)
        policy=sample_probe_policy(ssh_config=str(self.admission),identity_path=str(identity),
            component_paths=(('collector',str(source)),),timeout_ms=1000)
        bom=self.bom(data=data)
        bindings=({'component_id':'collector','absolute_path':str(source),'relative_path':'synthetic/collector.py'},)
        output=cli.observe((device,),registry_digest=self.registry.digest,policy=policy,
            scheduler_factory=self.scheduler_factory(),compare_components=compare_components,bom=bom,evidence_bindings=bindings)
        row=output['observations'][0]
        self.assertTrue(row['identity_verified']);self.assertEqual(row['generation'],1)
        self.assertGreaterEqual(row['fields']['ram_available_bytes']['value'],0)
        self.assertEqual(row['component_comparison']['collector']['source']['status'],'verified_files')
        self.assertEqual(row['component_comparison']['collector']['installed']['sha256'],hashlib.sha256(data).hexdigest())
        self.assertIs(row['component_comparison']['collector']['installed']['matches_source'],True)
        self.assertIsNone(row['component_comparison']['collector']['runtime']['sha256'])
        self.assertIsNone(row['fields']['controller_state']['value'])
        self.assertEqual(self.schedulers[0].stats()['spawned'],1)
        self.assertEqual(self.schedulers[0].stats()['children'],0)
        self.assertIn('PARTIAL_EVIDENCE',output['warning_codes'])
        # Equal hash does not authorize a different source label, or manufacture loaded bytes.
        result=sample_probe_result(registry_digest=self.registry.digest,fields={
            'installed_components':sample_evidence({'collector':{'status':'observed',
                'sha256':hashlib.sha256(data).hexdigest(),'source':'file:/synthetic/different.py'}}),
            'runtime_components':sample_evidence({'collector':{'status':'observed',
                'sha256':hashlib.sha256(data).hexdigest(),'source':'file:synthetic/collector.py'}})})
        bound=cli._bind_file_evidence(result,policy=policy,bom=bom,bindings=bindings)
        compared=compare_components(bom,bound)['components'][0]
        self.assertIsNone(compared['installed']['sha256']);self.assertIsNone(compared['runtime']['sha256'])
        with self.assertRaises(ValueError):
            cli._bind_file_evidence(result,policy=policy,bom=bom,bindings=[{**bindings[0],'relative_path':'wrong.py'}])

    def test_actual_six_local_namespaces_same_session_A_signatures_and_digest(self):
        from host.standardizer.tests.test_registry_adapter import context_at,signed_archive
        from host.standardizer import worker
        from host.qc.fleet_monitor.readonly_probe import _REMOTE_SCRIPT,_ssh_argv
        from tools.fleet_maintenance.component_bom import compare_components
        context=context_at(self.root,registry=self.registry)
        for device in self.registry.devices:
            path,source,_=signed_archive(context,device.device_id)
            self.assertEqual(worker.verify_receipt(path,context=context)[1],source)
            namespace=self.root/device.device_id;namespace.mkdir();(namespace/'identity').write_text(device.device_id)
        policy=sample_probe_policy(ssh_config=str(self.admission),component_paths=(),timeout_ms=1500)
        # Fake SSH models distinct remote filesystems, but uses the actual fixed reader and IPC.
        prefix=('import io,sys,json\np=json.load(sys.stdin)\np["identity_path"]='+repr(str(self.root))+
                '+"/"+p["device_id"]+"/identity"\nsys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(p).encode()))\n')
        def local_ssh(device,digest,generation,policy):
            _ssh_argv(device,policy)
            return subprocess.Popen([sys.executable,'-I','-S','-c',prefix+'exec('+repr(_REMOTE_SCRIPT)+')'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,close_fds=True)
        output=cli.observe(self.registry.devices,registry_digest=self.registry.digest,policy=policy,
            scheduler_factory=self.scheduler_factory(local_ssh),compare_components=compare_components,bom=self.bom())
        self.assertEqual(len(output['observations']),6)
        self.assertEqual({(r['device_id'],r['generation']) for r in output['observations']},
                         {(d.device_id,1) for d in self.registry.devices})
        self.assertTrue(all(r['identity_verified'] for r in output['observations']))
        self.assertEqual(self.schedulers[0].stats()['spawned'],6)
        for device in self.registry.devices:
            allowed=upload_allowed(self.registry,device.device_id,destination='EGOL2',
                                   now_utc=datetime(2026,11,13,tzinfo=timezone.utc))
            self.assertEqual(allowed,device.device_id in ('cp_0007','cp_0011','cp_0013','cp_0014'))

    def test_real_CLI_timeout_is_bounded_and_new_scheduler_cannot_retry(self):
        from host.qc.tests.test_governance_isolation import synthetic_factory
        from tools.fleet_maintenance.component_bom import compare_components
        device,=select_devices(self.registry,['cp_0042'],purpose='monitor')
        policy=sample_probe_policy(timeout_ms=40)
        factory=Mock(side_effect=synthetic_factory(hung=(device.device_id,)))
        start=time.monotonic()
        output=cli.observe((device,),registry_digest=self.registry.digest,policy=policy,
            scheduler_factory=self.scheduler_factory(factory),compare_components=compare_components,bom=self.bom())
        self.assertLess(time.monotonic()-start,3)
        self.assertTrue(output['cleanup_required']);self.assertIn('DEVICE_QUARANTINED',output['warning_codes'])
        again=cli.observe((device,),registry_digest=self.registry.digest,policy=policy,
            scheduler_factory=self.scheduler_factory(factory),compare_components=compare_components,bom=self.bom())
        self.assertEqual(factory.call_count,1)
        self.assertEqual(again['observations'][0]['status'],'quarantined')
        self.assertEqual(again['operator_action'],'NO_AUTOMATIC_RETRY_REVIEW_REMOTE_CLEANUP')

    def test_actual_builder_BOM_shape_and_candidate_metadata_mutations(self):
        from tools.fleet_maintenance.component_bom import build_bom
        from tools.fleet_maintenance.release_registry import resource_digest,validate_release
        blobs={name:(ROOT/name).read_bytes() for name in builder.PAYLOAD_PATHS}
        files=builder._assemble('a'*40,blobs,build_bom,resource_digest)
        envelope=json.loads(files['BOM.json']);components=envelope['component_BOM']['components']
        self.assertEqual(len(components),9)
        self.assertEqual(next(r for r in components if r['component_id']=='monitor')['source']['status'],'verified_files')
        self.assertTrue(all(r['runtime']['sha256'] is None and r['installed']['sha256'] is None for r in components))
        reasons=validate_release(release=json.loads(files['release.json']),router=json.loads(files['router.json']),
                                 resources=builder._candidate_resources(files))
        self.assertTrue(reasons)  # Candidate integrity never grants production approval.
        mutations={'base_commit':'b'*40,'scope':['collector'],'reason':'tampered','expiry':'2099-01-01T00:00:00Z',
                   'overlay_ids':['unregistered'],'release_id':'wrong','extra':True}
        for key,value in mutations.items():
            changed=dict(files);release=json.loads(files['release.json']);release[key]=value
            changed['release.json']=canonical_json(release).encode()
            if key=='release_id':changed['router.json']=canonical_json({value:release['resource_digest']}).encode()
            with self.subTest(key=key),self.assertRaises(ValueError):
                builder.verify_candidate(changed,resource_digest=resource_digest)

    def test_real_process_wrong_tuples_and_reader_identity_are_unknown(self):
        from host.qc.tests.test_governance_isolation import synthetic_factory
        from tools.fleet_maintenance.component_bom import compare_components
        for device,corrupt in zip(self.registry.devices[:3],(
                {'registry_digest':'f'*64},{'device_id':'cp_0043'},{'generation':2})):
            output=cli.observe((device,),registry_digest=self.registry.digest,
                policy=sample_probe_policy(timeout_ms=200),scheduler_factory=self.scheduler_factory(
                    synthetic_factory(corrupt=corrupt)),compare_components=compare_components,bom=self.bom())
            row=output['observations'][0]
            self.assertFalse(row['identity_verified'])
            self.assertTrue(all(f['value'] is None for f in row['fields'].values()))
            self.assertTrue(output['cleanup_required'])
        device,=select_devices(self.registry,['cp_0042'],purpose='monitor')
        identity=self.root/'wrong-identity';identity.write_text('cp_0043')
        output=cli.observe((device,),registry_digest=self.registry.digest,
            policy=sample_probe_policy(ssh_config=str(self.admission),identity_path=str(identity),
                                      component_paths=(),timeout_ms=500),
            scheduler_factory=self.scheduler_factory(),compare_components=compare_components,bom=self.bom())
        self.assertEqual(output['observations'][0]['status'],'identity_rejected')
        self.assertTrue(all(f['value'] is None for f in output['observations'][0]['fields'].values()))

    def test_real_child_false_close_main_reports_cleanup_and_retains_handle(self):
        from tools.fleet_maintenance.component_bom import compare_components
        from host.qc.tests.test_governance_isolation import synthetic_factory
        device,=select_devices(self.registry,['cp_0042'],purpose='monitor')
        wrappers=[];spawn=synthetic_factory(hung=(device.device_id,))
        class Unreaped:
            # Synthetic unreapable poll, real local child; not actual kernel D-state proof.
            def __init__(self,process):
                self.process=process;self.hold=True
                self.pid=process.pid;self.stdin=process.stdin;self.stdout=process.stdout;self.stderr=process.stderr
            @property
            def returncode(self):return self.process.returncode
            def poll(self):return None if self.hold else self.process.poll()
        def factory(*args):
            wrapper=Unreaped(spawn(*args));wrappers.append(wrapper);return wrapper
        args=['--registry','/synthetic/registry','--targets',device.device_id,'--policy','/synthetic/policy','--once']
        policy=sample_probe_policy(timeout_ms=30)
        try:
            with (patch.object(cli,'_read_json',side_effect=[registry_wire(self.registry),
                    json.loads(canonical_json(asdict(policy)))]),
                  patch.object(cli,'_adapters',return_value=(self.scheduler_factory(factory),compare_components)),
                  patch.object(cli,'_packaged_metadata',return_value=(self.bom(),())),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(cli.main(args),3)
            error=json.loads(errors.getvalue())
            self.assertEqual(error['error'],'CLOSE_NOT_REAPED');self.assertTrue(error['cleanup_required'])
            self.assertEqual(len(cli._UNCLOSED),1)
        finally:
            for wrapper in wrappers:wrapper.hold=False
            for scheduler in self.schedulers:self.assertTrue(scheduler.close())


class MainBCallerTests(unittest.TestCase):
    def test_missing_or_wrong_registry_plan_blocks_before_factory_build_or_stage(self):
        with patch.object(sys,'path',[str(ROOT/'tools/fleet_maintenance'),*sys.path]):
            operations=importlib.import_module('operations');fleet=importlib.import_module('fleet')
            online=importlib.import_module('online');web_config=importlib.import_module('web_config')
        registry=sample_registry(('cp_0042',))
        config=web_config.setup(registry=registry,selected=['cp_0042'],config={'ssh_config':'/synthetic/config'})
        base=fleet.bind_registry_plan({'device':'cp_0042','commit':fleet.PIN,'unknown_approved':False},
                                     registry=registry,device='cp_0042')
        wrong_identity=dict(base['registry_device'],ssh_alias='different-alias')
        plans=[{k:v for k,v in base.items() if k!='registry_digest'},
               dict(base,registry_digest='f'*64),dict(base,registry_device=wrong_identity)]
        with tempfile.TemporaryDirectory(prefix='governance-plan-binding-') as directory:
            state=Path(directory)
            for plan in plans:
                digest,_=online.write_plan(state,plan)
                factory=Mock(side_effect=AssertionError('SSH before plan binding'))
                with (patch.object(fleet,'build') as build,patch.object(online,'stage_and_apply') as stage,
                      self.assertRaises(ValueError)):
                    operations.execute(state,config,'cp_0042',digest,factory,registry=registry)
                factory.assert_not_called();build.assert_not_called();stage.assert_not_called()
        drift=dict(config,registry_digest='f'*64)
        with self.assertRaises(ValueError):operations.require_config(drift,registry)


class LintPremergeTests(unittest.TestCase):
    def scope(self, paths):
        return {'schema_version': 1, 'files': [
            {'path': name, 'category': 'runtime', 'reason': 'synthetic lint adversary'} for name in paths],
                'exceptions': [], 'exception_sources': {}, 'exception_reviews': {}}

    def test_detect_specific_compare_generated_list_default_and_ip(self):
        text = """TARGETS=frozenset('cp_'+str(n).zfill(4) for n in range(7,12))
def f(device='cp_0042'):
    return device == 'cp_0042'
ENDPOINT='203.0.113.8'
p.add_argument('--targets', default=['cp_0007'])
"""
        self.assertEqual({rule for rule, _, _ in lint._findings(text)},
                         {'DEVICE_TABLE', 'DEVICE_DEFAULT', 'DEVICE_COMPARE', 'RUNTIME_IP'})

    def test_path_first_decoys_zero_reads(self):
        with tempfile.TemporaryDirectory(prefix='governance-lint-') as directory:
            root = Path(directory)
            decoys = ('adapter/state/data.json', 'venv-runtime/key.py', 'private/config.json',
                      'integrations-generated/adapter.py', 'images/real.py', 'data/raw.py')
            for name in decoys:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('THIS MUST NOT BE PARSED')
            (root / 'keylink.py').symlink_to(root / decoys[0])
            (root / 'safe.py').write_text('value = 1\n')
            opened = []
            original = lint._read_source

            def spy(root, name):
                opened.append(name)
                return original(root, name)

            with patch.object(lint, '_read_source', side_effect=spy):
                result = lint.scan_scope(root, self.scope((*decoys, 'keylink.py', 'safe.py')))
            self.assertEqual(opened, ['safe.py'])
            self.assertTrue(result['passed'])
            self.assertEqual(len(result['excluded']), 7)

    def test_enumeration_finishes_before_any_read(self):
        with tempfile.TemporaryDirectory(prefix='governance-lint-') as directory:
            root = Path(directory)
            (root / 'safe.py').write_text('value=1')
            with patch.object(lint, '_read_source') as read, self.assertRaises(ValueError):
                lint.scan_scope(root, self.scope(('safe.py', '../unsafe.py')))
            read.assert_not_called()

    def test_exact_exception_and_no_values_output(self):
        with tempfile.TemporaryDirectory(prefix='governance-lint-') as directory:
            root = Path(directory)
            (root / 'test.py').write_text("TARGETS=['cp_0042']\nOTHER=['cp_0043']\n")
            scope = self.scope(('test.py',))
            scope['exceptions'] = [{'path': 'test.py', 'rule': 'DEVICE_TABLE', 'symbol': 'TARGETS',
                                    'category': 'fixture', 'reason': 'exact synthetic example only'}]
            scope['exception_sources'] = {'test.py': hashlib.sha256((root / 'test.py').read_bytes()).hexdigest()}
            scope['exception_reviews'] = {'test.py': {'owner': 'synthetic-reviewer', 'reason': 'fixture only',
                'reviewed_at': '2026-11-01T00:00:00Z', 'expiry': '2099-01-01T00:00:00Z'}}
            result = lint.scan_scope(root, scope)
            self.assertEqual(len(result['classified']), 1)
            self.assertEqual(len(result['findings']), 1)
            self.assertEqual(result['findings'][0]['symbol'], 'OTHER')
            self.assertNotIn('cp_004', canonical_json(result))
            expired = lint.scan_scope(root, scope, now_utc=datetime(2099, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(expired['classified'], [])
            self.assertEqual(len(expired['findings']), 2)
            (root / 'test.py').write_text("TARGETS=['cp_0042','cp_0043']\n")
            changed = lint.scan_scope(root, scope)
            self.assertEqual(changed['classified'], [])
            self.assertEqual(len(changed['findings']), 1)

    def test_missing_adapters_not_complete(self):
        with tempfile.TemporaryDirectory(prefix='governance-lint-') as directory:
            result = lint.scan_scope(directory, self.scope(('missing_adapter.py',)))
            self.assertFalse(result['integration_complete'])
            self.assertEqual(result['opened_files'], 0)


class BuilderPremergeTests(unittest.TestCase):
    def synthetic_blobs(self):
        # Deliberately synthetic immutable input; no claim of complete B/C runtime package.
        blobs = {name: b'# synthetic source\n' for name in builder.PAYLOAD_PATHS}
        blobs[builder._MANIFEST] = (ROOT / builder._MANIFEST).read_bytes()
        blobs[builder._PROVENANCE] = b'{"schema_version":1,"artifacts":[]}'
        return blobs

    def assemble(self):
        build_bom = Mock(return_value={'schema_version': 1, 'components': {}, 'synthetic': True})
        files = builder._assemble('a' * 40, self.synthetic_blobs(), build_bom, synthetic_resource_digest)
        self.assertEqual(set(build_bom.call_args.kwargs), {'source_manifest', 'read_blob', 'overlays'})
        source = build_bom.call_args.kwargs['source_manifest']
        self.assertEqual(set(source), {'schema_version', 'base_commit', 'components'})
        self.assertEqual(len(source['components']), 9)
        for row in source['components']:
            self.assertEqual(len(row['files']), len(builder.PAYLOAD_PATHS) if row['component_id'] == 'monitor' else 0)
        return files

    def test_deterministic_payload_bom_and_candidate_router_binding(self):
        first, second = self.assemble(), self.assemble()
        self.assertEqual(first, second)
        release, router = json.loads(first['release.json']), json.loads(first['router.json'])
        self.assertEqual(router[release['release_id']], synthetic_resource_digest(builder._candidate_resources(first)))
        self.assertEqual(release['approval']['status'], 'candidate')
        self.assertEqual(set(release), {'release_id', 'base_commit', 'overlay_ids', 'component_BOM',
                                       'scope', 'reason', 'expiry', 'approval', 'resource_digest'})
        with tarfile.open(fileobj=io.BytesIO(first['payload.tar.gz']), mode='r:gz') as archive:
            self.assertEqual(set(archive.getnames()), set(builder.PAYLOAD_PATHS) | {'BOM.json'})
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime, member.mode), (0, 0, 0, 0o644))
            self.assertEqual(archive.extractfile('BOM.json').read(), first['BOM.json'])
        self.assertEqual(first['payload.tar.gz'][4:8], b'\0\0\0\0')

    def test_unlisted_private_and_missing_sources_reject(self):
        blobs = self.synthetic_blobs()
        for bad in ({**blobs, 'private/config.json': b'synthetic'},
                    {key: value for key, value in blobs.items() if not key.endswith('readonly_probe.py')}):
            with self.assertRaises(ValueError):
                builder._assemble('a' * 40, bad, lambda **kwargs: {}, synthetic_resource_digest)
        for name in ('../secret', '/absolute', 'a/../b', 'a//b'):
            with self.assertRaises(ValueError):
                builder._safe_name(name)

    def test_tampered_router_resource_or_bom_blocked(self):
        files = self.assemble()
        for name in ('payload.tar.gz', 'BOM.json', 'release.json', 'router.json'):
            changed = dict(files)
            changed[name] = b'{}' if name.endswith('.json') else b'tampered-synthetic'
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError, tarfile.TarError)):
                builder.verify_candidate(changed, resource_digest=synthetic_resource_digest)

    def test_every_release_metadata_field_is_bound_to_payload(self):
        files = self.assemble()
        self.assertTrue(builder.verify_candidate(files, resource_digest=synthetic_resource_digest))
        mutations = {'base_commit': 'b' * 40, 'scope': ['upload'], 'reason': 'tampered',
                     'expiry': '2099-01-01T00:00:00Z', 'overlay_ids': ['unregistered'],
                     'release_id': 'different-candidate', 'unexpected': True,
                     'approval': {'status': 'approved'}, 'component_BOM': {},
                     'resource_digest': 'f' * 64}
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = dict(files)
                release = json.loads(files['release.json'])
                release[field] = value
                changed['release.json'] = canonical_json(release).encode()
                if field == 'release_id':
                    changed['router.json'] = canonical_json({value: release['resource_digest']}).encode()
                with self.assertRaises(ValueError):
                    builder.verify_candidate(changed, resource_digest=synthetic_resource_digest)

    def test_clean_synthetic_export_help_and_missing_targets(self):
        # Real main/C0 files, explicitly fake absent B/C adapters: parser proof only.
        blobs = self.synthetic_blobs()
        required = set(strict_json_loads(blobs[builder._MANIFEST].decode())['required_adapters'])
        for name in builder.PAYLOAD_PATHS:
            if name not in required and (ROOT / name).is_file():
                blobs[name] = (ROOT / name).read_bytes()
        files = builder._assemble('a' * 40, blobs, lambda **kwargs: {'synthetic': True}, synthetic_resource_digest)
        with tempfile.TemporaryDirectory(prefix='governance-export-') as directory:
            root = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(files['payload.tar.gz']), mode='r:gz') as archive:
                for member in archive.getmembers():
                    builder._safe_name(member.name)
                    self.assertTrue(member.isfile())
                    path = root / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.extractfile(member).read())
            env = {**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}
            for args, code in ((['--help'], 0), ([], 2)):
                process = subprocess.run([sys.executable, '-S', '-m',
                    'host.qc.fleet_monitor.governance_cli', *args], cwd=root, env=env,
                    capture_output=True, timeout=5)
                self.assertEqual(process.returncode, code)
                self.assertNotIn(b'ModuleNotFoundError', process.stderr)

    def test_new_output_only_symlink_and_traversal_rejected(self):
        with tempfile.TemporaryDirectory(prefix='governance-build-') as directory:
            root = Path(directory)
            files = self.assemble()
            builder._write_new(root / 'first', files)
            self.assertEqual((root / 'first/payload.tar.gz').read_bytes(), files['payload.tar.gz'])
            with self.assertRaises(FileExistsError):
                builder._write_new(root / 'first', files)
            (root / 'link').symlink_to(root / 'first', target_is_directory=True)
            with self.assertRaises(OSError):
                builder._write_new(root / 'link/new', files)
            with self.assertRaises(ValueError):
                builder._write_new(root / 'first/../escape', files)

    def test_git_symlink_not_read_and_missing_adapter_explicit(self):
        process = Mock(stdout=b'120000 blob synthetic\thost/qc/fleet_monitor/readonly_probe.py\n')
        with patch.object(subprocess, 'run', return_value=process) as run:
            with self.assertRaises(ValueError):
                builder._git_blob(ROOT, 'a' * 40, 'host/qc/fleet_monitor/readonly_probe.py')
            self.assertEqual(run.call_count, 1)
        with patch.object(subprocess, 'run', return_value=Mock(stdout=b'')):
            with self.assertRaisesRegex(builder.BuildError, '^ADAPTER_NOT_INTEGRATED$'):
                builder._git_blob(ROOT, 'a' * 40, 'host/qc/fleet_monitor/readonly_probe.py')


if __name__ == '__main__':
    unittest.main()
