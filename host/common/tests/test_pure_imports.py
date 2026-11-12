"""Import execution and API calls cannot open config, connect, or start workers."""
import builtins
import importlib
import io
import multiprocessing
import os
import pathlib
import socket
import subprocess
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import host.common.device_registry
import host.common.governance_contract
import host.common.strict_json
import host.qc.tests.governance_fixtures


class PureImportTests(unittest.TestCase):
    def test_no_runtime_io(self):
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError('Unexpected runtime side effect')

        with ExitStack() as stack:
            for obj, name in ((builtins, 'open'), (io, 'open'), (os, 'open'),
                              (pathlib.Path, 'read_text'), (pathlib.Path, 'read_bytes'),
                              (socket, 'socket'), (socket, 'getaddrinfo'),
                              (subprocess, 'Popen'), (threading.Thread, 'start'),
                              (multiprocessing.Process, 'start')):
                stack.enter_context(patch.object(obj, name, forbidden))
            # Python's loader reads source code; no application/config files may be opened.
            for name in ('host', 'host.common', 'host.qc', 'host.qc.tests',
                         'host.qc.fleet_monitor', 'host.standardizer',
                         'host.common.strict_json', 'host.common.device_registry',
                         'host.common.governance_contract', 'host.qc.tests.governance_fixtures'):
                module = importlib.import_module(name)
                saved = dict(module.__dict__)
                def restore(module=module, saved=saved):
                    module.__dict__.clear()
                    module.__dict__.update(saved)
                stack.callback(restore)
                importlib.reload(module)
            registry_api = importlib.import_module('host.common.device_registry')
            fixtures = importlib.import_module('host.qc.tests.governance_fixtures')
            registry = fixtures.sample_registry()
            registry_api.select_devices(registry, ['cp_0042'], purpose='monitor')
            registry_api.receive_ids(registry)
            registry_api.upload_allowed(registry, 'cp_0042', destination='EGOL2',
                                        now_utc=datetime(2026, 11, 13, tzinfo=timezone.utc))
            fixtures.sample_probe_policy()
            fixtures.sample_probe_result()
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
