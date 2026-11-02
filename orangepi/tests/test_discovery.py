"""Regression coverage for changed USB paths, duplicate IDs and frozen sessions."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import device_discovery as discovery


def candidate(kind, port, serial='200901010001'):
    return dict(kind=kind, path='/dev/by-path/' + port, topology=port,
                serial=serial, vendor='0bda' if kind == 'wrist' else '1a86',
                product='5858' if kind == 'wrist' else '55d3')


def config():
    return {'auto_discovery': {'enabled': True},
            'wrist_cameras': {
                'cam_left_wrist': {'label': '左腕相机', 'device': '/dev/old-left', 'width': 640, 'height': 480, 'fps': 30},
                'cam_right_wrist': {'label': '右腕相机', 'device': '/dev/old-right', 'width': 640, 'height': 480, 'fps': 30}},
            'emg': {'emg_left': {'label': '左臂肌电', 'port': '/dev/old-emg-left'},
                    'emg_right': {'label': '右臂肌电', 'port': '/dev/old-emg-right'}}}


class DiscoveryTests(unittest.TestCase):
    def test_stale_paths_resolve_two_distinct_cameras_without_guessing_sides(self):
        cameras = [candidate('wrist', '1.3.1'), candidate('wrist', '1.3.3')]
        result, state = discovery.resolve(config(), cameras, {})
        slots = list(result['wrist_cameras'].values())
        self.assertEqual({x['device'] for x in slots}, {x['path'] for x in cameras})
        self.assertTrue(all(x['discovery']['present'] for x in slots))
        self.assertTrue(all(not x['discovery']['role_confirmed'] for x in slots))
        self.assertIn('A', slots[0]['label'])

    def test_reenumeration_and_reversed_order_keep_assignments(self):
        cameras = [candidate('wrist', '1.3.1'), candidate('wrist', '1.3.3')]
        first, state = discovery.resolve(config(), cameras, {})
        second, _ = discovery.resolve(config(), list(reversed(cameras)), state)
        self.assertEqual(first, second)

    def test_reconnecting_only_b_does_not_relabel_it_a(self):
        cameras = [candidate('wrist', '1.3.1'), candidate('wrist', '1.3.3')]
        _, state = discovery.resolve(config(), cameras, {})
        result, state = discovery.resolve(config(), cameras[1:], state)
        self.assertFalse(result['wrist_cameras']['cam_left_wrist']['discovery']['present'])
        self.assertEqual(result['wrist_cameras']['cam_right_wrist']['device'], cameras[1]['path'])

    def test_moving_parent_hub_preserves_unique_port_suffix_but_not_physical_claim(self):
        old = [candidate('wrist', '1.4.3.1'), candidate('wrist', '1.4.3.3')]
        _, state = discovery.resolve(config(), old, {})
        new = [candidate('wrist', '9.6.3.1'), candidate('wrist', '9.6.3.3')]
        result, _ = discovery.resolve(config(), new, state)
        self.assertEqual(result['wrist_cameras']['cam_left_wrist']['device'], new[0]['path'])
        self.assertEqual(result['wrist_cameras']['cam_right_wrist']['device'], new[1]['path'])
        self.assertFalse(result['wrist_cameras']['cam_left_wrist']['discovery']['role_confirmed'])

    def test_unique_serial_follows_emg_across_ports(self):
        item = candidate('emg', '1.2', '68495')
        _, state = discovery.resolve(config(), [item], {})
        moved = candidate('emg', '5.9', '68495')
        result, _ = discovery.resolve(config(), [moved], state)
        self.assertEqual(result['emg']['emg_left']['port'], moved['path'])
        self.assertFalse(result['emg']['emg_right']['discovery']['present'])

    def test_extra_unknown_cameras_are_not_arbitrarily_recorded(self):
        cameras = [candidate('wrist', '1.3.' + str(i)) for i in range(3)]
        result, _ = discovery.resolve(config(), cameras, {})
        self.assertTrue(all(not x['discovery']['present'] for x in result['wrist_cameras'].values()))

    def test_config_is_not_mutated_and_old_known_identity_keeps_role(self):
        cfg = config(); original = copy.deepcopy(cfg)
        item = candidate('emg', '1.2', 'OLD')
        cfg['emg']['emg_left']['port'] = item['path']
        result, _ = discovery.resolve(cfg, [item], {})
        self.assertEqual(result['emg']['emg_left']['label'], '左臂肌电')
        self.assertTrue(result['emg']['emg_left']['discovery']['role_confirmed'])
        self.assertNotIn('discovery', cfg['emg']['emg_left'])

    def test_serial_mismatch_at_same_port_does_not_inherit_confirmed_side(self):
        cfg = config(); old = candidate('emg', '1.2', 'OLD')
        cfg['emg']['emg_left']['port'] = old['path']
        _, state = discovery.resolve(cfg, [old], {})
        result, _ = discovery.resolve(cfg, [candidate('emg', '1.2', 'NEW')], state)
        self.assertFalse(result['emg']['emg_left']['discovery']['role_confirmed'])

    def test_locked_registry_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cameras = [candidate('wrist', '1.3.1'), candidate('wrist', '1.3.3')]
            with patch.object(discovery, 'inventory', return_value=cameras):
                first = discovery.discover(config(), base)
            with patch.object(discovery, 'inventory', return_value=list(reversed(cameras))):
                self.assertEqual(first, discovery.discover(config(), base))
            self.assertTrue((base / 'runtime/device_bindings.json').is_file())


class ManualEmgDiscoveryTests(unittest.TestCase):
    def cfg(self):
        cfg = config(); cfg['emg_calibration'] = {'enabled': True, 'scope': 'boot'}
        return cfg

    def pair(self):
        return [candidate('emg', 'new-1', 'BRAND-NEW-1'), candidate('emg', 'new-2', 'BRAND-NEW-2')]

    def test_arbitrary_supported_unique_serials_are_temporary_not_body_sides(self):
        cfg = self.cfg(); original = copy.deepcopy(cfg)
        rows = self.pair(); result, state = discovery.resolve(cfg, rows, {})
        self.assertEqual(cfg, original)
        for index, slot in enumerate(('emg_left', 'emg_right')):
            item = result['emg'][slot]
            self.assertEqual(item['port'], rows[index]['path'])
            self.assertTrue(item['discovery']['present'])
            self.assertFalse(item['discovery']['role_confirmed'])
            self.assertEqual(item['discovery']['binding'], 'temporary')
            self.assertIn(chr(65+index), item['label'])
            self.assertFalse(state['slots'][slot]['role_confirmed'])
        reverse, _ = discovery.resolve(cfg, list(reversed(rows)), state)
        self.assertEqual(result, reverse)

    def test_fixed_roles_and_confirmed_history_cannot_override_manual_sources(self):
        cfg = self.cfg(); rows = self.pair()
        cfg['emg_serial_roles'] = {'emg_left': rows[1]['serial'], 'emg_right': rows[0]['serial']}
        cfg['emg']['emg_left']['port'] = rows[1]['path']
        registry = {'slots': {'emg_left': dict(rows[1], role_confirmed=True),
                              'emg_right': dict(rows[0], role_confirmed=True)}}
        result, _ = discovery.resolve(cfg, rows, registry)
        self.assertNotIn('emg_serial_roles', result)
        self.assertIn('emg_serial_roles', cfg)
        self.assertEqual(result['emg']['emg_left']['port'], rows[0]['path'])
        self.assertTrue(all(not item['discovery']['role_confirmed'] for item in result['emg'].values()))

    def test_extra_duplicate_missing_identity_or_node_alias_are_ambiguous(self):
        pair = self.pair()
        duplicate_serial = copy.deepcopy(pair); duplicate_serial[1]['serial'] = duplicate_serial[0]['serial']
        empty_serial = copy.deepcopy(pair); empty_serial[1]['serial'] = ''
        duplicate_path = copy.deepcopy(pair); duplicate_path[1]['path'] = duplicate_path[0]['path']
        node_alias = copy.deepcopy(pair)
        for row in node_alias: row['node'] = '/dev/ttyACM0'
        cases = [pair+[candidate('emg', 'new-3', 'BRAND-NEW-3')], duplicate_serial,
                 empty_serial, duplicate_path, node_alias]
        for rows in cases:
            with self.subTest(rows=rows):
                result, state = discovery.resolve(self.cfg(), rows, {'slots': {'emg_left': pair[0], 'emg_right': pair[1]}})
                for item in result['emg'].values():
                    self.assertFalse(item['discovery']['present'])
                    self.assertEqual(item['discovery']['reason'], 'ambiguous')
                    self.assertTrue(item['port'].startswith('/dev/emg-unassigned-'))
                self.assertNotIn('emg_left', state['slots'])
                self.assertNotIn('emg_right', state['slots'])

    def test_unsupported_usb_identity_is_not_assigned(self):
        rows = self.pair()
        for row in rows: row['product'] = '9999'
        result, _ = discovery.resolve(self.cfg(), rows, {})
        self.assertTrue(all(not item['discovery']['present'] for item in result['emg'].values()))
        self.assertTrue(all(item['discovery']['candidate_count'] == 0 for item in result['emg'].values()))

    def test_one_device_is_a_only_and_missing_ports_do_not_retain_old_targets(self):
        row = self.pair()[1]
        result, _ = discovery.resolve(self.cfg(), [row], {})
        self.assertEqual(result['emg']['emg_left']['port'], row['path'])
        self.assertTrue(result['emg']['emg_left']['discovery']['present'])
        self.assertFalse(result['emg']['emg_left']['discovery']['role_confirmed'])
        self.assertEqual(result['emg']['emg_right']['port'], '/dev/emg-unassigned-B')
        self.assertFalse(result['emg']['emg_right']['discovery']['present'])
        result, _ = discovery.resolve(self.cfg(), [], {'slots': {'emg_left': row}})
        self.assertEqual({item['port'] for item in result['emg'].values()},
                         {'/dev/emg-unassigned-A', '/dev/emg-unassigned-B'})

    def test_disabled_mode_keeps_existing_fixed_serial_contract(self):
        cfg = self.cfg(); cfg['emg_calibration']['enabled'] = False; rows = self.pair()
        cfg['emg_serial_roles'] = {'emg_left': rows[1]['serial'], 'emg_right': rows[0]['serial']}
        result, _ = discovery.resolve(cfg, rows, {})
        self.assertEqual(result['emg']['emg_left']['port'], rows[1]['path'])
        self.assertTrue(result['emg']['emg_left']['discovery']['role_confirmed'])


if __name__ == '__main__':
    unittest.main()
