"""Offline fixtures only: no hardware, network mutation, or system service calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'orangepi/src/orangepi_collect/common'))
import project_paths
import phone_transport as transport
from phone_links import PhoneLinks


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = script('phone_tether_profile')
installer = script('install_phone_transport')
CONFIG = {'mode': 'usb_tether', 'selector': {'usb_port': '1-2.3', 'vid': '18d1', 'pid': '4ee3', 'serial': 'PRIVATE-SERIAL'}}


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.net = self.base / 'net'
        self.net.mkdir()
        self.config = copy.deepcopy(CONFIG)

    def device(self, name='enxfixture', driver='rndis_host', port='1-2.3', vid='18d1', pid='4ee3'):
        parent = self.base / 'devices' / port
        parent.mkdir(parents=True, exist_ok=True)
        for key, value in [('idVendor', vid), ('idProduct', pid), ('serial', 'PRIVATE-SERIAL')]:
            (parent / key).write_text(value)
        device = parent / (port + ':' + name)
        device.mkdir()
        target = self.base / 'drivers' / driver
        target.mkdir(parents=True, exist_ok=True)
        (device / 'driver').symlink_to(target, target_is_directory=True)
        interface = self.net / name
        interface.mkdir()
        (interface / 'device').symlink_to(device, target_is_directory=True)
        return {'ifname': name, 'flags': ['LOWER_UP'], 'addr_info': [{'family': 'inet', 'local': '192.168.77.2'}]}

    def observe(self, rows):
        return transport.observe(self.config, rows, self.net)

    def cache(self, rows=None):
        rows = rows if rows is not None else [self.device()]
        result = self.observe(rows)
        value = dict(result, schema=1, boot_id='boot-fixture', monotonic=100.0,
                     observer_instance=str(uuid.uuid4()), selector=self.config['selector'], mode='usb_tether')
        file = self.base / 'cache.json'
        file.write_text(json.dumps(value))
        file.chmod(0o600)
        return file, value, rows

    def read(self, file, rows, now=101, boot='boot-fixture'):
        real_observe = transport.observe
        with patch.object(transport, 'ip_rows', return_value=rows), patch.object(transport, 'observe', side_effect=lambda config, data: real_observe(config, data, self.net)):
            return transport.read_cache(file, self.config, now, boot)


class ConfigTests(Fixtures):
    def test_default_missing_is_adb(self):
        self.assertEqual(transport.load_config(self.base / 'missing')['mode'], 'adb')

    def test_invalid_modes_and_unreviewed_selectors_rejected(self):
        for value in [{'mode': 'wifi'}, {'mode': None}, {'mode': 'usb_tether'}, {'mode': 'adb', 'token': 'secret'}, [], {'mode': 'usb_tether', 'selector': {'usb_port': '*'}}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                transport.validate_config(value)

    def test_optional_serial(self):
        del self.config['selector']['serial']
        self.assertEqual(transport.validate_config(self.config), self.config)


class ObserverTests(Fixtures):
    def test_rndis_and_ncm(self):
        for driver in transport.DRIVERS:
            row = self.device(name=driver, driver=driver)
            self.assertEqual(self.observe([row])['state'], 'addressed')

    def test_family_and_ordinary_usb_ethernet_excluded(self):
        family = {'ifname': 'wlan0', 'flags': ['LOWER_UP'], 'addr_info': [{'family': 'inet', 'local': '192.168.20.15'}]}
        ethernet = self.device('eth0', 'r8152')
        self.assertEqual(self.observe([family, ethernet])['state'], 'absent')

    def test_wrong_port_vid_pid_and_serial_excluded(self):
        row = self.device()
        for key, wrong in [('usb_port', '1-9'), ('vid', '0000'), ('pid', '0000'), ('serial', 'WRONG')]:
            config = copy.deepcopy(self.config)
            config['selector'][key] = wrong
            self.assertEqual(transport.observe(config, [row], self.net)['state'], 'absent')

    def test_multi_match_is_ambiguous_and_has_no_addresses(self):
        result = self.observe([self.device('netA'), self.device('netB')])
        self.assertEqual(result, {'state': 'ambiguous', 'reason': 'multiple_matches', 'links': []})

    def test_no_address_and_link_down(self):
        row = self.device()
        row['addr_info'] = []
        self.assertEqual(self.observe([row])['state'], 'no_address')
        row['flags'] = []
        self.assertEqual(self.observe([row])['links'], [])

    def test_unplug_and_rename_never_retain_old_address(self):
        old = self.device()
        self.assertEqual(self.observe([old])['state'], 'addressed')
        self.assertEqual(self.observe([])['links'], [])
        renamed = self.device('newname')
        result = self.observe([renamed])
        self.assertEqual(result['links'][0]['interface'], 'newname')

    def test_bad_paths_and_unusable_ips(self):
        for name in ('../../etc', '', '/etc/passwd'):
            self.assertEqual(self.observe([{'ifname': name}])['state'], 'absent')
        row = self.device()
        row['addr_info'] = [{'family': 'inet', 'local': x} for x in ('127.0.0.1', '169.254.1.2', '0.0.0.0', '224.0.0.1', 'broken')]
        self.assertEqual(self.observe([row])['state'], 'no_address')


class CacheTests(Fixtures):
    def test_valid_public_cache_has_no_secret(self):
        file, _, rows = self.cache()
        result = self.read(file, rows)
        self.assertEqual(result['state'], 'addressed')
        self.assertNotIn('PRIVATE-SERIAL', json.dumps(result))
        self.assertNotIn('selector', result)

    def test_ttl_boot_and_future_timestamp_fail_closed(self):
        file, _, rows = self.cache()
        for now, boot in ((106, 'boot-fixture'), (99, 'boot-fixture'), (101, 'other-boot')):
            self.assertEqual(self.read(file, rows, now, boot)['state'], 'stale')

    def test_invalid_schema_instance_selector_and_payload(self):
        file, value, rows = self.cache()
        for field, invalid in [('schema', 99), ('schema', True), ('monotonic', True), ('observer_instance', 'bad'), ('selector', {}), ('state', 'ready'), ('links', [{'kind': 'wireless'}])]:
            bad = dict(value, **{field: invalid})
            file.write_text(json.dumps(bad))
            self.assertEqual(self.read(file, rows)['state'], 'stale')

    def test_invalid_json_oversize_permissions_and_symlink(self):
        file, value, rows = self.cache()
        for text in ('{', 'x' * 33000, 'null', '[]'):
            file.write_text(text)
            self.assertEqual(self.read(file, rows)['state'], 'stale')
        file.write_text(json.dumps(value))
        for mode in (0o666, 0o644, 0o640):
            file.chmod(mode)
            self.assertEqual(self.read(file, rows)['state'], 'stale')
        file.chmod(0o600)
        self.base.chmod(0o755)
        self.assertEqual(self.read(file, rows)['state'], 'stale')
        self.base.chmod(0o700)
        link = self.base / 'symlink'
        link.symlink_to(file)
        self.assertEqual(self.read(link, rows)['state'], 'stale')

    def test_changed_address_unplug_and_rename_invalidate_fresh_cache(self):
        file, _, rows = self.cache()
        self.assertEqual(self.read(file, [])['state'], 'stale')
        rows[0]['addr_info'][0]['local'] = '192.168.78.2'
        self.assertEqual(self.read(file, rows)['state'], 'stale')
        self.assertEqual(self.read(file, [self.device('renamed')])['state'], 'stale')

    def test_atomic_writer_requires_private_directory(self):
        directory = self.base / 'runtime'
        directory.mkdir(mode=0o700)
        target = directory / 'link.json'
        with patch.object(transport, 'boot_id', return_value='fixture'):
            transport.write_cache(target, self.config, self.observe([]), str(uuid.uuid4()))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(directory.iterdir())), 1)
        directory.chmod(0o755)
        with self.assertRaises(ValueError):
            transport.write_cache(target, self.config, self.observe([]), str(uuid.uuid4()))


class ProfileTests(Fixtures):
    def generate(self, **changes):
        args = dict(config=self.config, id_path='platform-fd800000.usb-usb-0:2.3:1.0', driver='rndis_host',
                    nm_version='1.42.0', subnet='192.168.77.0/24', routes=[{'dst': '192.168.20.0/24'}, {'dst': 'default'}])
        args.update(changes)
        with patch('subprocess.check_output', side_effect=AssertionError('no commands')), patch('subprocess.run', side_effect=AssertionError('no commands')):
            return profile.generate(**args)

    def test_narrow_match_and_route_dns_ipv6_autoconnect_guards(self):
        text = self.generate()
        for expected in ('path=platform-fd800000.usb-usb-0:2.3:1.0;', 'driver=rndis_host;', 'autoconnect=false', 'never-default=true', 'ignore-auto-dns=true', 'ignore-auto-routes=true', '[ipv6]\nmethod=disabled'):
            self.assertIn(expected, text)
        self.assertEqual(text, self.generate())

    def test_unsupported_version_unknown_path_driverwide_and_overlap_blocked(self):
        for change in ({'nm_version': '1.20'}, {'nm_version': 'unknown'}, {'id_path': '*usb*'}, {'id_path': 'pci-0000:00:1.0'}, {'driver': '*'}, {'routes': [{'dst': '192.168.77.0/24'}]}, {'routes': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.generate(**change)


class PlanTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.config_path = self.base / 'config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.fixture = self.base / 'target-fixture'
        self.fixture.mkdir()
        self.staging = self.base / 'plan'

    def plan(self):
        with patch('subprocess.run', side_effect=AssertionError('no service calls')):
            return installer.plan(self.staging, self.config_path, self.fixture)

    def test_plan_backup_idempotence_and_no_install(self):
        target = self.fixture / 'etc/headring-phone-transport.json'
        target.parent.mkdir()
        target.write_text('{"mode":"adb"}')
        target.chmod(0o640)
        result = self.plan()
        self.assertEqual(result['status'], 'plan_only_not_installed')
        self.assertEqual(result, self.plan())
        entry = next(x for x in result['files'] if x['target'] == '/etc/headring-phone-transport.json')
        self.assertEqual(entry['before']['mode'], 0o640)
        self.assertEqual((self.staging / entry['backup']).read_bytes(), target.read_bytes())
        self.assertEqual(installer.rollback_plan(self.staging)['actions'], [])
        self.assertEqual(target.read_text(), '{"mode":"adb"}')

    def test_target_modification_conflict_refused(self):
        self.plan()
        target = self.fixture / 'etc/headring-phone-transport.json'
        target.parent.mkdir()
        target.write_text('later operator change')
        with self.assertRaises(ValueError):
            installer.rollback_plan(self.staging)

    def test_payload_modification_conflict_refused(self):
        self.plan()
        (self.staging / 'payload/0').write_text('modified')
        with self.assertRaises(ValueError):
            installer.rollback_plan(self.staging)

    def test_symlink_staging_and_system_target_refused(self):
        link = self.base / 'linked'
        link.symlink_to(self.fixture, target_is_directory=True)
        for path in (link, '/etc/new-plan'):
            with self.assertRaises(ValueError):
                installer.plan(path, self.config_path)

    def test_mode_gates_have_no_conflicts_or_service_mutation(self):
        unit = (ROOT / 'deployment/systemd/headring-phone-tether.service').read_text()
        self.assertIn('User=orangepi', unit)
        self.assertIn('CapabilityBoundingSet=\n', unit)
        self.assertIn('--require usb_tether', unit)
        self.assertNotIn('Conflicts=', unit)
        dropin = (ROOT / 'deployment/phone-tether/20-transport-mode.conf').read_text()
        self.assertIn('--require adb', dropin)
        for word in ('nmcli', 'adb shell', "'stop'", "'start'"):
            self.assertNotIn(word, (ROOT / 'services/phone-tether/transport_config.py').read_text())


class LinkTests(unittest.TestCase):
    def owner(self):
        from types import SimpleNamespace
        return SimpleNamespace(server=SimpleNamespace(server_port=8765), boot_id='boot', privacy=SimpleNamespace(snapshot=lambda: {'instance': 'controller-fixture'}))

    def test_socket_classification_and_legacy_fields(self):
        rows = [{'kind': 'wireless', 'interface': 'wlan0', 'address': '192.168.20.15', 'origin': 'http://192.168.20.15:8765'}]
        links = PhoneLinks(self.owner(), provider=lambda port: rows)
        with patch('phone_links.load_config', return_value={'mode': 'adb', 'selector': {}}):
            value = links.snapshot(('127.0.0.1', 8765))
            self.assertEqual(value['current_transport'], 'loopback')
            self.assertEqual(value['mode'], 'wifi')
            self.assertEqual(value['links'][0]['interface'], 'wlan0')
            self.assertEqual(links.snapshot(('192.168.20.15', 8765))['current_transport'], 'wireless')

    def test_usb_only_fresh_addresses_and_identity(self):
        links = PhoneLinks(self.owner(), provider=lambda port: self.fail('no WiFi fallback'))
        result = {'state': 'addressed', 'reason': 'pi_address_only', 'links': [{'kind': 'usb_tether', 'interface': 'enxfixture', 'address': '192.168.77.2'}]}
        with patch('phone_links.load_config', return_value=CONFIG), patch('phone_links.read_cache', return_value=result):
            value = links.snapshot(('192.168.77.2', 8765))
            self.assertEqual(value['current_transport'], 'usb_tether')
            self.assertEqual(value['controller_identity'], 'controller-fixture')
            self.assertIsNone(value['device_id'])
            self.assertNotIn('PRIVATE-SERIAL', json.dumps(value))
            self.assertEqual(links.snapshot(('192.168.20.15', 8765))['current_transport'], 'unknown')

    def test_invalid_config_is_error_not_adb_fallback(self):
        with patch('phone_links.load_config', side_effect=ValueError()):
            value = PhoneLinks(self.owner()).snapshot(('127.0.0.1', 8765))
            self.assertEqual(value['state'], 'error')
            self.assertEqual(value['links'], [])

    def test_existing_device_id_read_is_bounded_and_unknown_is_null(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            file = Path(directory) / 'DEVICE_ID'
            self.assertIsNone(transport.device_id(file))
            file.write_text('cp_0015\n')
            self.assertEqual(transport.device_id(file), 'cp_0015')
            file.write_text('secret\nsecond-line')
            self.assertIsNone(transport.device_id(file))


class CLITests(Fixtures):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / 'services/phone-tether'))
        self.addCleanup(sys.path.remove, str(ROOT / 'services/phone-tether'))
        import transport_config
        import tether_link
        self.gate = transport_config
        self.observer = tether_link

    def test_startup_mode_gate_validates_without_mutation(self):
        for configured, required, expected in [('adb', 'adb', 0), ('adb', 'usb_tether', 1), ('usb_tether', 'adb', 1), ('usb_tether', 'usb_tether', 0)]:
            with patch.object(sys, 'argv', ['gate', '--require', required]), patch.object(self.gate, 'load_config', return_value={'mode': configured}), patch.object(self.gate, 'opposite_inactive', return_value=True):
                self.assertEqual(self.gate.main(), expected)

    def test_active_or_unknown_opposite_mode_refused_without_stopping(self):
        from types import SimpleNamespace
        for state, allowed in [('active', False), ('activating', False), ('deactivating', False), ('', False), ('inactive', True), ('failed', True)]:
            with patch.object(self.gate.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=state)) as run:
                self.assertEqual(self.gate.opposite_inactive('usb_tether'), allowed)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', 'usb-phone-link.service'])

    def test_status_is_read_only_and_public(self):
        import io
        result = {'state': 'stale', 'reason': 'cache_unavailable_or_invalid', 'links': []}
        with patch.object(sys, 'argv', ['observer', 'status']), patch.object(self.observer, 'load_config', return_value=self.config), patch.object(self.observer, 'read_cache', return_value=result), patch.object(self.observer, 'write_cache', side_effect=AssertionError('no cache writes')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(self.observer.main(), 0)
            self.assertNotIn('PRIVATE-SERIAL', output.getvalue())
            self.assertEqual(json.loads(output.getvalue()), result)

    def test_root_writer_refused_before_network_observation(self):
        import io
        with patch.object(sys, 'argv', ['observer', '--once']), patch.object(self.observer, 'load_config', return_value=self.config), patch.object(self.observer.os, 'getuid', return_value=0), patch.object(self.observer, 'ip_rows', side_effect=AssertionError('must refuse before observation')), patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(self.observer.main(), 2)


if __name__ == '__main__':
    unittest.main()
