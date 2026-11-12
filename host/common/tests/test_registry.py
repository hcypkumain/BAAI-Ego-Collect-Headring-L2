import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from host.common.device_registry import (Device, UploadGrant, parse_registry,
                                         receive_ids, select_devices, upload_allowed)
from host.common.strict_json import canonical_json, strict_json_loads, utc_timestamp
from host.qc.tests.governance_fixtures import (LEGACY_UPLOAD_IDS, sample_device,
                                              sample_grants, sample_registry)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def serialized(registry):
    return json.loads(canonical_json({'schema_version': 1,
                                     'devices': [asdict(d) for d in registry.devices]}))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = sample_registry()
        self.value = serialized(self.registry)

    def test_digest_and_frozen(self):
        self.assertEqual(self.registry.digest, hashlib.sha256(
            canonical_json(self.value).encode()).hexdigest())
        self.assertEqual(parse_registry(self.value), self.registry)
        with self.assertRaises(FrozenInstanceError):
            self.registry.devices[0].enrolled = False
        with self.assertRaises(ValueError):
            replace(self.registry, digest='0' * 64)
        with self.assertRaises(ValueError):
            parse_registry(json.dumps(self.value))
        self.assertEqual(parse_registry({'schema_version': 1, 'devices': []}).devices, ())

    def test_unknown_fields_and_strict_types(self):
        for location in ('root', 'device', 'grant'):
            value = copy.deepcopy(self.value)
            obj = value if location == 'root' else value['devices'][0]
            if location == 'grant':
                obj = obj['upload_grants'][0]
            obj['extra'] = None
            with self.subTest(location=location), self.assertRaises(ValueError):
                parse_registry(value)
        for field, bad in [('enrolled', 1), ('receive_member', 'true'),
                           ('monitor_enabled', None), ('device_id', 'cp_42'),
                           ('device_id', 'cp_0042\n'), ('ssh_alias', '-option'),
                           ('address', 'example.invalid'), ('address', 'fe80::1%eth0'),
                           ('capabilities', ['probe', 'probe']),
                           ('capabilities', [True]), ('ssh_user', '')]:
            value = copy.deepcopy(self.value)
            value['devices'][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                parse_registry(value)
        for bad in (True, '1', 1.0, 2):
            with self.subTest(version=bad), self.assertRaises(ValueError):
                parse_registry({**self.value, 'schema_version': bad})

    def test_duplicate_identities(self):
        for field in ('device_id', 'ssh_alias', 'address', 'host_key_alias'):
            value = copy.deepcopy(self.value)
            value['devices'][1][field] = value['devices'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_registry(value)
        for field in ('ssh_alias', 'host_key_alias'):
            value = copy.deepcopy(self.value)
            value['devices'][1][field] = value['devices'][0]['device_id']
            with self.subTest(cross_namespace=field), self.assertRaises(ValueError):
                parse_registry(value)
            value['devices'][1][field] = value['devices'][0]['address']
            with self.subTest(address_alias=field), self.assertRaises(ValueError):
                parse_registry(value)
        value = copy.deepcopy(self.value)
        value['devices'][0]['address'] = '2001:db8::1'
        value['devices'][1]['address'] = '2001:0db8:0:0:0:0:0:1'
        with self.assertRaises(ValueError):
            parse_registry(value)
        value['devices'][0]['address'] = '192.0.2.1'
        value['devices'][1]['address'] = '::ffff:192.0.2.1'
        with self.assertRaises(ValueError):
            parse_registry(value)
        value['devices'][1]['address'] = '2001:db8::2'
        self.assertEqual(len(parse_registry(value).devices), 6)

    def test_cap_and_selection(self):
        self.assertEqual(len(sample_registry(tuple('cp_%04d' % i for i in range(64))).devices), 64)
        with self.assertRaises(ValueError):
            sample_registry(tuple('cp_%04d' % i for i in range(65)))
        for purpose in ('monitor', 'receive', 'standardize'):
            self.assertEqual(select_devices(self.registry, ['cp_0042'], purpose=purpose)[0].device_id,
                             'cp_0042')
            for selection in ([], ['cp_0043'], ['cp_0042', 'cp_0042'], 'cp_0042', [True]):
                with self.subTest(purpose=purpose, selected=selection), self.assertRaises(ValueError):
                    select_devices(self.registry, selection, purpose=purpose)
        with self.assertRaises(ValueError):
            select_devices(self.registry, ['cp_0042'], purpose='upload')
        self.assertIn('cp_0042', receive_ids(self.registry))
        registry = sample_registry(receive=(), monitor=(), enrolled=())
        self.assertEqual(receive_ids(registry), frozenset())
        for purpose in ('monitor', 'receive', 'standardize'):
            with self.assertRaises(ValueError):
                select_devices(registry, ['cp_0042'], purpose=purpose)
        registry = sample_registry(receive=(), monitor=('cp_0042',))
        self.assertEqual(len(select_devices(registry, ['cp_0042'], purpose='monitor')), 1)
        with self.assertRaises(ValueError):
            select_devices(registry, ['cp_0042'], purpose='standardize')
        registry = sample_registry(monitor=())
        self.assertEqual(len(select_devices(registry, ['cp_0042'], purpose='receive')), 1)
        with self.assertRaises(ValueError):
            select_devices(registry, ['cp_0042'], purpose='monitor')

    def test_grants_independent_exact_scope(self):
        for did in LEGACY_UPLOAD_IDS:
            for destination in ('EGOL2', 'EGOL3_hand'):
                self.assertTrue(upload_allowed(self.registry, did, destination=destination, now_utc=NOW))
        for did in ('cp_0042', 'cp_0015', 'cp_0043'):
            self.assertFalse(upload_allowed(self.registry, did, destination='EGOL2', now_utc=NOW))
        for target in ('*', 'EGOL2/*', 'EGOL2/cp_0007/session', 'EGOL2evil',
                       'EGOL2/../EGOL3_hand', '', None):
            self.assertFalse(upload_allowed(self.registry, 'cp_0007', destination=target, now_utc=NOW))
        self.assertFalse(upload_allowed(sample_registry(grants={}), 'cp_0007',
                                        destination='EGOL2', now_utc=NOW))
        self.assertFalse(upload_allowed(sample_registry(enrolled=()), 'cp_0007',
                                        destination='EGOL2', now_utc=NOW))
        self.assertTrue(upload_allowed(sample_registry(receive=(), monitor=()), 'cp_0007',
                                       destination='EGOL2', now_utc=NOW))
        for now in (datetime(2026, 11, 13), NOW.astimezone(timezone(timedelta(hours=1)))):
            with self.assertRaises(ValueError):
                upload_allowed(self.registry, 'cp_0007', destination='EGOL2', now_utc=now)

    def test_grant_validation_and_expiry(self):
        grant = sample_grants()['cp_0007'][0]
        for field, bad in [('operation', 'delete'), ('destination', '*'), ('reason', ''),
                           ('owner', ''), ('expiry', '2026-11-13'),
                           ('expiry', '2026-11-13T00:00:00+01:00'),
                           ('expiry', '2026-02-30T00:00:00Z'), ('reason', 'legacy: '),
                           ('reason', 'implicit permission')]:
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                UploadGrant(**{**grant, field: bad})
        for timestamp, allowed in [('2026-11-12T00:00:00Z', False),
                                   ('2026-11-13T00:00:00Z', False),
                                   ('2026-11-13T00:00:01+00:00', True)]:
            registry = sample_registry(grants={'cp_0007': [{**grant, 'expiry': timestamp,
                                                           'reason': 'explicit bounded scope'}]})
            self.assertEqual(upload_allowed(registry, 'cp_0007', destination='EGOL2', now_utc=NOW),
                             allowed)
        with self.assertRaises(ValueError):
            sample_registry(grants={'cp_0007': [grant, grant]})
        with self.assertRaises(ValueError):
            replace(sample_device(), capabilities=['probe'])

    def test_strict_json_entry(self):
        for payload in ('{"a":1,"a":2}', '{"a":{"x":0,"x":1}}', 'NaN', 'Infinity',
                        '-Infinity', '1e999', '"\\ud800"'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                strict_json_loads(payload)
        self.assertEqual(strict_json_loads('{"b":2,"a":1}'), {'a': 1, 'b': 2})

    def test_schema_example_and_semantic_validation(self):
        schema = json.loads((ROOT / 'contracts/v1/device-registry.schema.json').read_text())
        example = json.loads((ROOT / 'configs/device-registry.example.json').read_text())
        registry = parse_registry(example)
        self.assertFalse(registry.devices[0].enrolled)
        self.assertEqual(receive_ids(registry), frozenset())
        try:
            import jsonschema
        except ImportError:
            self.skipTest('Optional jsonschema unavailable; pure parser tests still run')
        checker = jsonschema.FormatChecker()
        # Some installed jsonschema builds lack the optional RFC3339 dependency.
        @checker.checks('date-time', raises=ValueError)
        def utc_format(value):
            utc_timestamp(value)
            return True
        validator = jsonschema.Draft202012Validator(schema, format_checker=checker)
        validator.check_schema(schema)
        validator.validate(example)
        validator.validate(self.value)
        for field, bad in [('enrolled', 1), ('address', 'example.invalid'),
                           ('device_id', 'cp_42')]:
            value = copy.deepcopy(example)
            value['devices'][0][field] = bad
            self.assertFalse(validator.is_valid(value))
        for grant in ({**sample_grants()['cp_0007'][0], 'reason': 'legacy: '},
                      {**sample_grants()['cp_0007'][0], 'expiry': '2026-02-30T00:00:00Z'}):
            value = copy.deepcopy(example)
            value['devices'][0]['upload_grants'] = [grant]
            self.assertFalse(validator.is_valid(value))


if __name__ == '__main__':
    unittest.main()
