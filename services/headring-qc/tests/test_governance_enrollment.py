"""Synthetic inventory, real durable Store and authenticated handoff regressions."""
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import importlib
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/headring-qc'))
sys.path.insert(0, str(ROOT / 'orangepi/src/orangepi_collect/common'))
from receiver_state import ReceiverStore
from handoff_receiver import HandoffReceiver
from cloud_upload_cli import UploadQueue, upload_allowed
from host.common.device_registry import parse_registry
from host.qc.tests.governance_fixtures import sample_registry, sample_grants
from tools.migrate_device_registry import migrate_legacy

DEVICE_IDS = ('cp_0014', 'cp_0015', 'cp_0018', 'cp_0020', 'cp_0022', 'cp_0042')
REGISTRY = sample_registry(DEVICE_IDS)
SESSION = 'L3-2026-11-13-40177'
NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ReceiverStore(self.root / 'ledger', registry=REGISTRY)

    def enqueue(self, did, sid=SESSION):
        return self.store.enqueue(did, dict(session_id=sid, native_sha256='a' * 64))

    def test_add_42_registry_only_unknown_43_denied_cross_device_isolation(self):
        ids = [self.enqueue(did) for did in DEVICE_IDS]
        self.assertEqual(len(set(ids)), len(DEVICE_IDS))
        self.assertEqual([self.store.job(i)['device'] for i in ids], list(DEVICE_IDS))
        with self.assertRaises(ValueError):
            self.enqueue('cp_0043')
        for i in ids:
            job = self.store.job(i)
            self.assertEqual(job['cloud_state'], 'not_configured')
            self.assertTrue(job['retention_hold'])

    def test_revoked_membership_blocks_old_queued_claims(self):
        self.enqueue('cp_0042')
        denied = sample_registry(DEVICE_IDS, receive=())
        store = ReceiverStore(self.store.root, registry=denied)
        store.set_control(True, 0, uuid.uuid4().hex)
        self.assertIsNone(store.claim('receive'))
        with self.assertRaises(ValueError):
            store.enqueue('cp_0042', {'session_id': SESSION})

    def test_same_device_claims_serialize_other_device_progresses(self):
        self.enqueue('cp_0042')
        self.enqueue('cp_0042', 'L3-2026-11-13-40178')
        self.enqueue('cp_0020')
        other = ReceiverStore(self.store.root, registry=REGISTRY)
        self.store.set_control(True, 0, uuid.uuid4().hex)
        barrier = threading.Barrier(2)
        claimed = []
        def claim(store):
            barrier.wait()
            claimed.append(store.claim('receive'))
        threads = [threading.Thread(target=claim, args=(store,)) for store in (self.store, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual({row['device'] for row in claimed}, {'cp_0042', 'cp_0020'})
        self.assertIsNone(self.store.claim('receive'))
        first = next(row for row in claimed if row['device'] == 'cp_0042')
        self.store.update(first['id'], receive_state='host_committed')
        self.assertEqual(self.store.claim('receive')['device'], 'cp_0042')

    def test_handoff_uses_same_registry_locks_and_filters_legacy(self):
        legacy = types.SimpleNamespace(ROOT=self.root, DATA=self.root / 'data', STOP=threading.Event(),
                    CONFIG={'devices': [{'id': did} for did in DEVICE_IDS + ('cp_0043',)]})
        receiver = HandoffReceiver(legacy, registry=REGISTRY)
        self.assertEqual(set(receiver.devices), set(DEVICE_IDS))
        self.assertEqual(set(receiver.pi_locks), set(DEVICE_IDS))
        self.assertIs(receiver.store.registry, REGISTRY)
        for did in DEVICE_IDS:
            receiver.store.enqueue(did, {'session_id': SESSION})
        with self.assertRaises(TypeError):
            HandoffReceiver(legacy)

    def test_upload_scope_grants_expiry_and_source_mismatch(self):
        registry = sample_registry()
        for did in sample_grants():
            for project in ('EGOL2', 'EGOL3_hand'):
                self.assertTrue(upload_allowed(f'{project}/{did}/2026-11-13/{SESSION}', registry=registry,
                                               now_utc=NOW, device_id=did))
        for did in ('cp_0042', 'cp_0043'):
            self.assertFalse(upload_allowed(f'EGOL2/{did}/{SESSION}', registry=registry, now_utc=NOW))
        for target in ('EGOL2/cp_0007', 'EGOL2/../cp_0007/package', 'EGOL2evil/cp_0007/package'):
            with self.assertRaises(ValueError):
                upload_allowed(target, registry=registry, now_utc=NOW)
        with self.assertRaises(ValueError):
            upload_allowed('EGOL2/cp_0007/package', registry=registry, now_utc=NOW, device_id='cp_0042')
        grants = sample_grants()
        grants['cp_0007'][0]['expiry'] = '2026-11-13T00:00:00Z'
        expired = sample_registry(grants=grants)
        self.assertFalse(upload_allowed('EGOL2/cp_0007/package', registry=expired, now_utc=NOW))
        disabled = sample_registry(enrolled=())
        self.assertFalse(upload_allowed('EGOL2/cp_0007/package', registry=disabled, now_utc=NOW))

    def test_queue_source_binding_and_stats_without_queue_constructor_or_credentials(self):
        std = self.root / 'standardized'
        std.mkdir()
        source = std / 'cp_0007' / '2026-11-13' / SESSION
        source.mkdir(parents=True)
        adapter = types.SimpleNamespace(std=std, registry=sample_registry())
        adapter._authorized = lambda target, source: UploadQueue._authorized(adapter, target, source)
        target = f'EGOL2/cp_0007/2026-11-13/{SESSION}'
        self.assertTrue(adapter._authorized(target, source))
        for bad in (f'EGOL2/cp_0014/2026-11-13/{SESSION}', 'EGOL2/cp_0007/other'):
            with self.assertRaises(ValueError):
                adapter._authorized(bad, source)
        denied = std / 'cp_0042' / SESSION
        denied.mkdir(parents=True)
        self.assertFalse(adapter._authorized('EGOL2/cp_0042/' + SESSION, denied))
        adapter.db = sqlite3.connect(':memory:')
        self.addCleanup(adapter.db.close)
        adapter.db.execute('CREATE TABLE uploads(target,source,state)')
        adapter.db.executemany('INSERT INTO uploads VALUES(?,?,?)',
            [(target, str(source), 'queued'), ('EGOL2/cp_0042/' + SESSION, str(denied), 'queued')])
        adapter.state = self.root / 'status'
        value = UploadQueue.status(adapter)
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['denied'], 1)
        self.assertEqual(value['counts'], {'queued': 1})
        adapter._require_allowed = lambda target, source: UploadQueue._require_allowed(adapter, target, source)
        with self.assertRaises(PermissionError):
            # Authorization fails before importing external SDK or opening credentials.
            UploadQueue.run_one(adapter, ('EGOL2/cp_0042/' + SESSION, str(denied), 'L3', '[]', 'queued', None, None, None))

    def test_migration_is_pure_explicit_not_union_or_default_grants(self):
        original = sample_registry(DEVICE_IDS)
        legacy = {'devices': []}
        for device in original.devices:
            row = asdict(device)
            row['id'] = row.pop('device_id')
            row['enabled'] = row['id'] != 'cp_0015'
            legacy['devices'].append(row)
        with patch.object(Path, 'open', side_effect=AssertionError('migration I/O')):
            result = migrate_legacy(legacy, receive_members=['cp_0042', 'cp_0015'], grants={})
        migrated = parse_registry(result['registry'])
        self.assertEqual(len(result['diff']), len(DEVICE_IDS))
        self.assertEqual({d.device_id for d in migrated.devices if d.enrolled and d.receive_member}, {'cp_0042'})
        self.assertTrue(all(not d.upload_grants for d in migrated.devices))
        with self.assertRaises(ValueError):
            migrate_legacy(legacy, receive_members=['cp_0043'], grants={})

    def test_seed_claim_and_upload_recheck_grants_without_real_queue(self):
        std = self.root / 'standardized'
        denied = std / 'cp_0042' / SESSION
        denied.mkdir(parents=True)
        catalog = {'root': str(std), 'errors': [], 'entries': [
            {'relative_path': 'cp_0042/' + SESSION, 'base_profile': 'L3'}]}
        (std / '_catalog.json').write_text(json.dumps(catalog))
        adapter = types.SimpleNamespace(std=std, app=self.root / 'app', state=self.root / 'upload-status',
                                        registry=sample_registry())
        adapter.db = sqlite3.connect(':memory:')
        self.addCleanup(adapter.db.close)
        adapter.db.execute('CREATE TABLE uploads(target TEXT PRIMARY KEY,source,profile,manifest,state,task_id,receipt,error)')
        adapter._authorized = lambda target, source: UploadQueue._authorized(adapter, target, source)
        adapter._require_allowed = lambda target, source: UploadQueue._require_allowed(adapter, target, source)
        adapter.status = lambda: UploadQueue.status(adapter)
        with patch('cloud_upload_cli.files_for_package', side_effect=AssertionError('unauthorized package opened')):
            UploadQueue.seed(adapter)
        self.assertEqual(adapter.db.execute('SELECT count(*) FROM uploads').fetchone()[0], 0)
        target = 'EGOL3_hand/cp_0042/' + SESSION
        adapter.db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)',
                           (target, str(denied), 'L3', '[]', 'queued', None, None, None))
        adapter.run_one = lambda row: self.fail('denied queue row claimed')
        value = UploadQueue.run(adapter)
        self.assertFalse(value['all_confirmed'])
        self.assertEqual(value['denied'], 1)
        with self.assertRaises(PermissionError):
            UploadQueue._safe_batch(adapter, None, str(denied), target, [], None)

    def test_migration_preserves_only_explicit_four_device_scopes(self):
        registry = sample_registry()
        legacy = {'devices': [dict(asdict(device), enabled=True) for device in registry.devices]}
        grants = sample_grants()
        grants['cp_0007'][0]['expiry'] = '2027-01-01T00:00:00Z'
        migrated = migrate_legacy(legacy, receive_members=['cp_0042'], grants=grants)
        parsed = parse_registry(migrated['registry'])
        for device in parsed.devices:
            self.assertEqual([asdict(g) for g in device.upload_grants], grants.get(device.device_id, []))
        self.assertFalse(upload_allowed('EGOL2/cp_0042/package', registry=parsed, now_utc=NOW))


# Run the existing certified raw-signature/portable-input/deletion recovery tests
# unchanged, adapting only construction in this A-owned suite. No auth is mocked.
legacy_handoff_tests = importlib.import_module('services.headring-qc.tests.test_handoff_receiver')


class PreservedHandoffTests(legacy_handoff_tests.Tests):
    def setUp(self):
        with patch.object(legacy_handoff_tests, 'HandoffReceiver', partial(HandoffReceiver, registry=REGISTRY)):
            super().setUp()
        self.key = secrets.token_bytes(32)
        (self.app / 'secrets/cp_0020').write_bytes(self.key)


if __name__ == '__main__':
    unittest.main()
