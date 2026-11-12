import json
import re
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from host.common.governance_contract import (COMPONENT_IDS, FIELD_IDS, Evidence,
                                             validate_evidence, validate_probe_policy,
                                             validate_probe_result)
from host.common.strict_json import canonical_json, strict_json_loads
from host.qc.tests.governance_fixtures import (sample_evidence, sample_probe_policy,
                                              sample_probe_result)


def wire(value):
    return json.loads(canonical_json(asdict(value)))


class ContractTests(unittest.TestCase):
    def test_documented_serialized_examples(self):
        path = Path(__file__).resolve().parents[3] / 'docs/governance/CONTRACT.md'
        examples = re.findall(r'```json\n(.*?)\n```', path.read_text(), re.DOTALL)
        self.assertEqual(len(examples), 2)
        components = strict_json_loads(examples[0])
        sample_probe_result(fields={'runtime_components': sample_evidence(components)})
        self.assertFalse(validate_probe_result(strict_json_loads(examples[1])).identity_verified)

    def test_roundtrip(self):
        evidence = sample_evidence(1024, source='proc:meminfo')
        self.assertEqual(validate_evidence(wire(evidence)), evidence)
        result = sample_probe_result(fields={'ram_available_bytes': evidence})
        self.assertEqual(validate_probe_result(wire(result)), result)
        policy = sample_probe_policy()
        self.assertEqual(validate_probe_policy(wire(policy)), policy)
        value = wire(policy)
        del value['timeout_ms']
        del value['maximum_bytes']
        self.assertEqual(validate_probe_policy(value), policy)
        for obj, validator in ((evidence, validate_evidence), (result, validate_probe_result),
                               (policy, validate_probe_policy)):
            with self.assertRaises(ValueError):
                validator({**wire(obj), 'unexpected': None})

    def test_evidence_strictness(self):
        observed = sample_evidence('boot', source='proc:boot_id')
        for changes in ({'value_json': 'null'}, {'value_json': 'NaN'},
                        {'value_json': '{"a":1,"a":2}'}, {'value_json': '{ "a": 1 }'},
                        {'quality': 'unknown'}, {'quality': 'error'},
                        {'quality': 'invented'}, {'observed_at': None}, {'source': ''},
                        {'observed_at': '2026-11-13'}, {'reason': 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(observed, **changes)
        self.assertIsNone(sample_evidence().value_json)
        self.assertEqual(replace(observed, quality='stale').value_json, '"boot"')

    def test_result_field_types(self):
        for name, good, bads in (
            ('boot_id', 'boot-uuid', (1, {}, True)),
            ('controller_state', 'busy', (0, {}, False)),
            ('ram_available_bytes', 0, (-1, True, 1.5, '12')),
            ('disk_available_bytes', 1024, (-1, False, 2.0, '12'))):
            self.assertEqual(dict(sample_probe_result(fields={name: sample_evidence(good)}).fields)[name],
                             sample_evidence(good))
            for bad in bads:
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    sample_probe_result(fields={name: sample_evidence(bad)})

    def test_component_evidence(self):
        unknown = {name: {'sha256': None, 'status': 'unknown', 'source': 'fixture:not-loaded'}
                   for name in COMPONENT_IDS}
        installed = {'collector': {'sha256': 'a' * 64, 'status': 'observed',
                                    'source': 'file:collector.py'}}
        result = sample_probe_result(fields={'installed_components': sample_evidence(installed),
                                             'runtime_components': sample_evidence(unknown)})
        self.assertNotEqual(dict(result.fields)['installed_components'],
                            dict(result.fields)['runtime_components'])
        for bad in ([], {'other': installed['collector']},
                    {'collector': {**installed['collector'], 'extra': 1}},
                    {'collector': {**installed['collector'], 'sha256': None}},
                    {'collector': {**installed['collector'], 'status': 'unknown'}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sample_probe_result(fields={'runtime_components': sample_evidence(bad)})

    def test_identity_and_result_shape(self):
        result = sample_probe_result(identity_verified=False, status='identity_rejected')
        self.assertTrue(all(e.value_json is None for _, e in result.fields))
        with self.assertRaises(ValueError):
            sample_probe_result(identity_verified=False,
                                fields={'ram_available_bytes': sample_evidence(1)})
        for changes in ({'generation': True}, {'generation': -1}, {'identity_verified': 1},
                        {'device_id': 'cp_42'}, {'registry_digest': 'no'}, {'status': 'idle'},
                        {'fields': result.fields[:-1]},
                        {'fields': result.fields[:-1] + (result.fields[0],)},
                        {'fields': list(result.fields)}, {'status': 'ok'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(result, **changes)
        with self.assertRaises(ValueError):
            sample_probe_result(status='identity_rejected', identity_verified=True)
        self.assertEqual(tuple(name for name, _ in result.fields), FIELD_IDS)
        with self.assertRaises(ValueError):
            sample_probe_result(fields={'typo': sample_evidence()})

    def test_policy_limits_and_opaque_paths(self):
        policy = sample_probe_policy()
        self.assertIsNone(policy.disk_mount)
        self.assertEqual(len(policy.component_paths), 9)
        for changes in ({'timeout_ms': True}, {'timeout_ms': 0}, {'timeout_ms': 5001},
                        {'maximum_bytes': 1048577}, {'maximum_bytes': False},
                        {'ssh_config': 'relative'}, {'known_hosts': '/a/../b'},
                        {'identity_path': '/a\ncommand'}, {'disk_mount': '//volume'},
                        {'component_paths': (('unknown', '/synthetic/file'),)},
                        {'component_paths': (('collector', '/a'), ('collector', '/b'))},
                        {'component_paths': (('collector', '/a'), ('helper', '/a'))}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(policy, **changes)
        self.assertEqual(replace(policy, component_paths=()).component_paths, ())
        self.assertEqual(replace(policy, disk_mount='/synthetic/volume').disk_mount,
                         '/synthetic/volume')


if __name__ == '__main__':
    unittest.main()
