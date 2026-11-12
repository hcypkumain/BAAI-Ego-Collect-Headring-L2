"""Real loopback HTTP fixture: authentication and local socket versus Host spoof."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orangepi_collect/common'))
import project_paths
import dashboard


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / 'runtime').mkdir()
        self.owner = dashboard.Dashboard(base, base, {'host': '127.0.0.1', 'port': 0})
        self.addCleanup(self.owner.close)
        self.url = 'http://127.0.0.1:' + str(self.owner.server.server_port) + '/api/phone/links'

    def test_unauthenticated_and_invalid_token_rejected(self):
        for token in (None, 'reference-not-control'):
            headers = {'X-Access-Token': token} if token else {}
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(self.url, headers=headers), timeout=3)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()

    def test_host_spoof_does_not_classify_transport_or_poison_origins(self):
        with patch('phone_links.load_config', return_value={'mode': 'adb', 'selector': {}}), patch.object(self.owner.phone_links, 'provider', return_value=[]):
            with urlopen(Request(self.url, headers={'X-Access-Token': self.owner.token, 'Host': '192.168.77.2:8765'}), timeout=3) as response:
                value = json.load(response)
        self.assertEqual(value['current_transport'], 'loopback')
        self.assertEqual(value['configured_transport'], 'adb')
        for field in ('mode', 'current', 'links', 'boot_id', 'controller_identity', 'device_id'):
            self.assertIn(field, value)
        self.assertNotIn(self.owner.token, json.dumps(value))


if __name__ == '__main__':
    unittest.main()
