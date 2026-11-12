"""Offline authenticated archive tests; converter execution is explicitly synthetic."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import hmac
import importlib
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from host.common.strict_json import canonical_json
from host.qc.tests.governance_fixtures import sample_registry
from host.standardizer import worker

DEVICE_IDS = ('cp_0014', 'cp_0015', 'cp_0018', 'cp_0020', 'cp_0022', 'cp_0042')
SESSION = 'L3-2026-11-13-40177'


def context_at(root, registry=None):
    data = root / 'data'
    data.mkdir(exist_ok=True)
    qc = root / 'qc'
    (qc / 'secrets').mkdir(parents=True, exist_ok=True)
    (qc / 'server-identity.json').write_text('{"server_id":"synthetic-server"}')
    return worker.make_context(registry=registry or sample_registry(DEVICE_IDS),
                               config={'data_root': str(data)}, qc_root=qc, data_root=data,
                               state_root=data / 'state', log_root=data / 'logs')


def signed_archive(context, did='cp_0042', sid=SESSION):
    source = context.data_root / 'archive' / '2026-11-13' / did / sid
    source.mkdir(parents=True)
    payload = b'synthetic archive, not a real MF format'
    (source / 'frames.bin').write_bytes(payload)
    files = [dict(path='frames.bin', bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())]
    value = dict(purpose='host_qc_archive_v1', device_id=did, session_id=sid,
                 server_id='synthetic-server', passed=True, archive_verified=True,
                 archive_path=str(source), manifest=dict(files=files, total_files=1,
                 total_bytes=len(payload), digest=hashlib.sha256(worker.canonical(files)).hexdigest()))
    key = secrets.token_bytes(32)
    (context.qc_root / 'secrets' / did).write_bytes(key)
    value['signature'] = hmac.new(key, worker.canonical(value), hashlib.sha256).hexdigest()
    path = context.data_root / 'receipts' / did / (sid + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path, source, key


def resign(path, key, **changes):
    value = worker.load(path)
    value.pop('signature')
    value.update(changes)
    value['signature'] = hmac.new(key, worker.canonical(value), hashlib.sha256).hexdigest()
    path.write_text(json.dumps(value))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = context_at(self.root)

    def test_import_and_context_no_application_io(self):
        registry = sample_registry(DEVICE_IDS)
        def denied(*args, **kwargs):
            raise AssertionError('application I/O during import/context')
        with patch('builtins.open', denied), patch('io.open', denied), \
             patch.object(Path, 'open', denied), patch.object(Path, 'resolve', denied), \
             patch.object(Path, 'read_text', denied), patch.object(Path, 'read_bytes', denied), \
             patch.object(sqlite3, 'connect', denied), patch.object(socket, 'socket', denied), \
             patch.object(subprocess, 'Popen', denied), patch.object(threading.Thread, 'start', denied):
            importlib.reload(worker)
            importlib.reload(worker.storage())
            ctx = worker.make_context(registry=registry, config={'data_root': '/synthetic/data'},
                                      qc_root=Path('/synthetic/qc'), data_root=Path('/synthetic/data'),
                                      state_root=Path('/synthetic/state'), log_root=Path('/synthetic/log'))
        with self.assertRaises(FrozenInstanceError):
            ctx.data_root = Path('/other')

    def test_config_is_frozen_copy_and_mismatched_root_denied(self):
        config = self.context.config
        config['data_root'] = '/other'
        self.assertNotEqual(self.context.config['data_root'], '/other')
        with self.assertRaises(ValueError):
            worker.make_context(registry=self.context.registry, config=config,
                                qc_root=self.context.qc_root, data_root=self.context.data_root,
                                state_root=self.context.state_root, log_root=self.context.log_root)

    def test_registry_only_42_pass_unknown_43_and_receive_false_deny(self):
        path, source, _ = signed_archive(self.context)
        self.assertEqual(worker.verify_receipt(path, context=self.context)[1], source)
        denied = replace(self.context, registry=sample_registry(DEVICE_IDS, receive=()))
        with self.assertRaises(ValueError):
            worker.verify_receipt(path, context=denied)
        unknown, _, _ = signed_archive(self.context, 'cp_0043')
        with self.assertRaises(ValueError):
            worker.verify_receipt(unknown, context=self.context)
        self.assertEqual(worker.make_one(unknown, context=self.context)['state'], 'ignored_device')

    def test_same_session_cross_device_keeps_archive_and_state_keys_separate(self):
        for did in DEVICE_IDS:
            path, source, _ = signed_archive(self.context, did)
            self.assertEqual(worker.verify_receipt(path, context=self.context)[1], source)
            self.assertEqual(worker.state_path(did, SESSION, context=self.context).parent.name, did)

    def test_authentication_not_mocked_signature_purpose_server_and_device_path(self):
        path, source, key = signed_archive(self.context)
        original = path.read_text()
        for changes in ({'signature': '0' * 64}, {'purpose': 'other'}, {'server_id': 'other'},
                        {'archive_path': str(source.parent.parent / 'cp_0020' / SESSION)},
                        {'passed': False}, {'archive_verified': False}):
            with self.subTest(changes=tuple(changes)):
                path.write_text(original)
                if 'signature' in changes:
                    value = worker.load(path)
                    value.update(changes)
                    path.write_text(json.dumps(value))
                else:
                    resign(path, key, **changes)
                with self.assertRaises(ValueError):
                    worker.verify_receipt(path, context=self.context)

    def test_archive_tampering_symlink_and_manifest_escape(self):
        path, source, key = signed_archive(self.context)
        (source / 'frames.bin').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            worker.verify_receipt(path, context=self.context)
        (source / 'frames.bin').unlink()
        (source / 'frames.bin').symlink_to(path)
        with self.assertRaises(ValueError):
            worker.verify_receipt(path, context=self.context)
        value = worker.load(path)
        value['manifest']['files'][0]['path'] = '../escape'
        resign(path, key, manifest=value['manifest'])
        with self.assertRaises(ValueError):
            worker.verify_receipt(path, context=self.context)

    def test_strict_json_duplicate_and_nan_rejected(self):
        path = self.root / 'bad.json'
        for text in ('{"device_id":"cp_0042","device_id":"cp_0043"}', '{"x":NaN}'):
            path.write_text(text)
            with self.assertRaises(ValueError):
                worker.load(path)

    def test_synthetic_executor_handoff_and_state_never_authorizes_upload(self):
        path, source, _ = signed_archive(self.context)
        calls = []
        def execute(command, **kwargs):
            calls.append(command)
            self.assertIn(str(source), command)
            target = Path(command[command.index('--out') + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_name(target.name + '.receipt.json').write_text(canonical_json(
                {'source_digest': worker.load(path)['manifest']['digest']}))
            kwargs['stdout'].write('EXPORT_RESULT ' + canonical_json(
                {'validation': {'passed': True}, 'seconds': 0}) + '\n')
            return types.SimpleNamespace(returncode=0)
        ctx = replace(self.context, executor=execute)
        # Synthetic free-space evidence only; real mount/UUID guard remains called.
        with patch.object(worker.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=40 * 1024**3)):
            row = worker.make_one(path, context=ctx)
        self.assertEqual(row['state'], 'complete', row)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('cloud_confirmed_at', row)
        self.assertEqual(worker.snapshot(context=ctx)['counts'], {'complete': 1})

    def test_storage_guard_failure_prevents_executor(self):
        path, _, _ = signed_archive(self.context)
        config = dict(self.context.config, storage_uuid='SYNTHETIC', storage_mount=str(self.root / 'not-mounted'))
        ctx = replace(self.context, config_json=canonical_json(config),
                      executor=lambda *a, **k: self.fail('converter ran without storage'))
        row = worker.make_one(path, context=ctx)
        self.assertEqual(row['state'], 'blocked')
        self.assertIn('STORAGE_UNAVAILABLE', row['reason'])

    def test_explicit_storage_relocation_preserves_signed_receipt(self):
        path, source, key = signed_archive(self.context)
        old_root = '/synthetic/retired-data-root'
        old_archive = old_root + '/archive/2026-11-13/cp_0042/' + SESSION
        resign(path, key, archive_path=old_archive)
        before = path.read_bytes()
        config = dict(self.context.config, storage_relocations={old_root: str(self.context.data_root)})
        ctx = replace(self.context, config_json=canonical_json(config))
        with patch.object(worker.storage(), 'configuration', side_effect=AssertionError('implicit config read')):
            self.assertEqual(worker.verify_receipt(path, context=ctx)[1], source)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
