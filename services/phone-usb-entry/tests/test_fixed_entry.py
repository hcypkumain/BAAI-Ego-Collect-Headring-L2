"""Offline policy and real mature-library packet tests; no LAN/mDNS launch."""
import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = load("fixed_entry", HERE / "fixed_entry.py")
builder = load("build_phone_usb_entry", REPO / "scripts/build_phone_usb_entry.py")
SELECTOR = {"usb_port": "4-2.3", "vid": "1234", "pid": "abcd"}


def snapshot():
    return {"usb": [dict(SELECTOR, driver="rndis_host", interface="usb9", ifindex=12,
                         generation=1, carrier=True)],
            "ip": [{"ifname": "usb9", "ifindex": 12, "flags": ["UP", "LOWER_UP"],
                    "addr_info": [{"family": "inet", "scope": "global", "local": "192.0.2.10"}]},
                   {"ifname": "Family", "ifindex": 2, "flags": ["UP", "LOWER_UP"],
                    "addr_info": [{"family": "inet", "scope": "global", "local": "198.51.100.1"}]}]}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.socket = self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("no socket")))
        self.popen = self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no subprocess")))
        self.fork = self.stack.enter_context(patch("os.fork", side_effect=AssertionError("no fork")))

    def test_default_and_publish_always_gate_before_io(self):
        for argv in ([], ["--publish"], ["--publish", "--duration", "600"],
                     ["--publish", "--duration", "0"], ["--publish", "--duration", "601"],
                     ["--publish", "--duration", "-1", "--check", "--selector", "/DO_NOT_READ"]):
            with self.subTest(argv=argv), patch.object(entry, "private_read", side_effect=AssertionError("no reads")) as read:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(entry.main(argv), 2)
                self.assertEqual(json.loads(output.getvalue())["reason"], "ISOLATION_UNAVAILABLE")
                read.assert_not_called()
        self.socket.assert_not_called()
        self.popen.assert_not_called()
        self.fork.assert_not_called()

    def test_permanent_not_supported(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            entry.main(["--publish", "--permanent", "true"])
        self.assertEqual(caught.exception.code, 2)

    def test_strict_json_and_schema(self):
        for raw in ('{"vid":1,"vid":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{'):
            with self.subTest(raw=raw), self.assertRaises(entry.Invalid):
                entry.strict_json(raw)
        for config in ([], dict(SELECTOR, extra=1), dict(SELECTOR, vid=1234),
                       dict(SELECTOR, pid=True), dict(SELECTOR, serial=None),
                       dict(SELECTOR, usb_port="usb9"), dict(SELECTOR, serial="SECRET\n")):
            with self.subTest(config=config), self.assertRaises(entry.Invalid):
                entry.selector(config)
        with self.assertRaises(entry.Invalid):
            entry.strict_json(" " * 65537)

    def test_identity_derivation_not_device_specific(self):
        for digits in ("0000", "0021", "1234", "9999"):
            self.assertEqual(entry.hostname("cp_" + digits), "cp-" + digits + ".local.")
        for name in ("CP_0021", "cp_21", "cp_0021\n", "headring", True, "cp_１２３４"):
            with self.assertRaises(entry.Invalid):
                entry.hostname(name)

    def test_private_path_mode_owner_symlink_budget(self):
        with tempfile.TemporaryDirectory(prefix="8788-", dir="/tmp/opencode") as root:
            root = Path(root)
            file = root / "opaque.json"
            file.write_text('{"synthetic":true}')
            file.chmod(0o600)
            self.assertEqual(entry.strict_json(entry.private_read(file)), {"synthetic": True})
            file.chmod(0o644)
            with self.assertRaises(entry.Invalid):
                entry.private_read(file)
            file.chmod(0o600)
            (root / "link").symlink_to(file)
            with self.assertRaises(entry.Invalid):
                entry.private_read(root / "link")
            (root / "dirlink").symlink_to(root, target_is_directory=True)
            with self.assertRaises(entry.Invalid):
                entry.private_read(root / "dirlink" / "opaque.json")
            with self.assertRaises(entry.Invalid):
                entry.private_read(str(root / "dirlink") + "/../opaque.json")
            actual_fstat = os.fstat
            def wrong_owner(fd):
                info = actual_fstat(fd)
                return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode)
            with patch("os.fstat", side_effect=wrong_owner), self.assertRaises(entry.Invalid):
                entry.private_read(file)
            with self.assertRaises(entry.Invalid):
                entry.private_read(file, 2)
            root.chmod(0o755)
            with self.assertRaises(entry.Invalid):
                entry.private_read(file)
            root.chmod(0o700)

    def test_selector_rejections_and_family_never_qualified(self):
        self.assertEqual(entry.qualify(SELECTOR, snapshot())["IP"], "192.0.2.10")
        mutations = [lambda s: s["usb"].clear(),
                     lambda s: s["usb"].append(dict(s["usb"][0], interface="usb10")),
                     lambda s: s["usb"][0].update(vid="ffff"),
                     lambda s: s["usb"][0].update(driver="iwlwifi"),
                     lambda s: s["usb"][0].update(carrier=False),
                     lambda s: s["usb"][0].update(ifindex=True),
                     lambda s: s["ip"][0].update(ifindex=99),
                     lambda s: s["ip"][0].update(flags=["UP"]),
                     lambda s: s["ip"][0].update(addr_info=[]),
                     lambda s: s["ip"][0]["addr_info"].append(dict(s["ip"][0]["addr_info"][0])),
                     lambda s: s["ip"][0]["addr_info"][0].update(local="127.0.0.1"),
                     lambda s: s["ip"][0]["addr_info"][0].update(tentative=True)]
        for mutate in mutations:
            data = snapshot()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(entry.Invalid):
                entry.qualify(SELECTOR, data)
        self.assertEqual(entry.qualify(dict(SELECTOR, serial="synthetic"),
                         dict(snapshot(), usb=[dict(snapshot()["usb"][0], serial="synthetic")]))["interface"], "usb9")
        with self.assertRaises(entry.Invalid):
            entry.qualify(dict(SELECTOR, serial="wrong"), snapshot())

    def test_offline_lifecycle_model_revoke_first(self):
        old = entry.qualify(SELECTOR, snapshot())
        self.assertEqual(entry.lifecycle_plan(None, None), [])
        self.assertEqual(entry.lifecycle_plan(old, copy.deepcopy(old)), [])
        self.assertEqual(entry.lifecycle_plan(old, None), ["unregister-old", "close-old"])
        for key, value in (("IP", "192.0.2.20"), ("ifindex", 13), ("generation", 2), ("interface", "usb10")):
            current = dict(old, **{key: value})
            self.assertEqual(entry.lifecycle_plan(old, current),
                             ["unregister-old", "close-old", "bind-explicit-ipv4-only", "register-no-name-change"])
        # Link down/plug removed/ambiguous -> caller drops old immediately;
        # real live watcher, conflict registration, and cleanup remain UNIMPLEMENTED.

    def test_public_output_redacted_and_check_not_ready(self):
        inputs = [b"cp_1234\n", json.dumps(dict(SELECTOR, serial="SECRET")).encode(),
                  json.dumps(dict(snapshot(), usb=[dict(snapshot()["usb"][0], serial="SECRET")])).encode()]
        with patch.object(entry, "private_read", side_effect=inputs), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main(["--check", "--device-id", "opaque", "--selector", "opaque", "--snapshot", "opaque"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"name", "interface", "IP", "generation", "state", "reason"})
        self.assertNotIn("SECRET", output.getvalue())
        self.assertEqual(result["name"], "cp-1234.local.")
        self.assertEqual(result["reason"], entry.BLOCKER)


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vendor = os.environ.get("PHONE_USB_ENTRY_VENDOR")
        if not vendor:
            raise unittest.SkipTest("explicit offline Python closure required via PHONE_USB_ENTRY_VENDOR")
        sys.path.insert(0, vendor)
        import zeroconf
        import ifaddr
        import async_timeout
        assert zeroconf.__version__ == "0.136.2"
        cls.zc = zeroconf
        for module in (zeroconf, ifaddr, async_timeout):
            if not Path(module.__file__).resolve().is_relative_to(Path(vendor).resolve()):
                raise AssertionError("dependency escaped explicit closure")

    def test_python38_syntax_and_no_compiled_extensions(self):
        root = Path(os.environ["PHONE_USB_ENTRY_VENDOR"])
        for source in list(root.rglob("*.py")) + [HERE / "fixed_entry.py"]:
            ast.parse(source.read_text(), filename=str(source), feature_version=(3, 8))
        self.assertEqual(list(root.rglob("*.so")), [])

    def test_actual_library_wildcard_and_missing_interface_guards(self):
        from zeroconf._utils import net
        sockets = []
        class RecordedSocket:
            def __init__(self, *args):
                self.options = []
                sockets.append(self)
            def setsockopt(self, *args):
                self.options.append(args)
            def bind(self, address):
                self.bound = address
            def fileno(self):
                return 123
        with patch("socket.socket", RecordedSocket), patch("subprocess.Popen", side_effect=AssertionError("no fork")):
            listener, responders = net.create_sockets(interfaces=["192.0.2.10"], ip_version=self.zc.IPVersion.V4Only)
        self.assertEqual(len(sockets), 2)
        self.assertEqual(listener.bound, ("", 5353))
        self.assertEqual(responders[0].bound, ("192.0.2.10", 5353))
        self.assertIn((socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("192.0.2.10")), responders[0].options)
        for sock in sockets:
            self.assertFalse(any(o[:2] == (socket.IPPROTO_IP, 49) for o in sock.options))  # Linux IP_MULTICAST_ALL
            self.assertFalse(any(o[:2] == (socket.SOL_SOCKET, socket.SO_BINDTODEVICE) for o in sock.options))

    def test_real_library_host_A_AAAA_and_unicast_egress_counterexample(self):
        from zeroconf._cache import DNSCache
        from zeroconf._core import async_send_with_transport
        from zeroconf._handlers.query_handler import QueryHandler
        from zeroconf._history import QuestionHistory
        from zeroconf._protocol.incoming import DNSIncoming
        from zeroconf._services.registry import ServiceRegistry
        zc = SimpleNamespace(registry=ServiceRegistry(), cache=DNSCache(), question_history=QuestionHistory(),
                             out_queue=Mock(), out_delay_queue=Mock(), async_send=Mock())
        with patch("socket.socket", side_effect=AssertionError("no socket")), patch("os.fork", side_effect=AssertionError("no fork")):
            info = entry.service_info("cp-1234.local.", entry.qualify(SELECTOR, snapshot()))
            self.assertEqual(info.server, "cp-1234.local.")
            self.assertEqual(info.port, 8765)
            self.assertEqual(info.properties, {b"path": b"/phone"})
            zc.registry.async_add(info)  # memory only; NOT registration or publication
            handler = QueryHandler(zc)
            wildcard = SimpleNamespace(is_ipv6=False, sock_name=("0.0.0.0", 5353), fileno=123, transport=Mock())
            for rrtype in (1, 28):  # direct hostname A and AAAA, not just DNS-SD
                zc.async_send.reset_mock()
                query = self.zc.DNSOutgoing(0)
                query.add_question(self.zc.DNSQuestion(info.server, rrtype, 1))
                handler.handle_assembled_query([DNSIncoming(query.packets()[0])], "198.51.100.77", 49152, wildcard, ())
                args = next(call.args for call in zc.async_send.call_args_list if len(call.args) == 5)
                self.assertIs(args[4], wildcard)
                packets = args[0].packets()
                answers = [r for packet in packets for r in DNSIncoming(packet).answers()]
                addresses = [r for r in answers if r.type in (1, 28)]
                if rrtype == 1:
                    self.assertEqual([(r.name, socket.inet_ntoa(r.address)) for r in addresses], [(info.server, "192.0.2.10")])
                else:
                    self.assertEqual(addresses, [])  # NSEC for absent AAAA, no non-USB/IPv6 leakage
                    self.assertTrue(any(r.type == 47 for r in answers))
                # Actual upstream public send pipeline accepts another network's
                # unicast destination on the wildcard receive transport.
                async_send_with_transport(False, wildcard, packets[0], 0, args[0], args[1], args[2], ())
                self.assertEqual(wildcard.transport.sendto.call_args.args[1], ("198.51.100.77", 49152))
            self.assertNotIn(b"198.51.100.1", b"".join(packets))


if __name__ == "__main__":
    unittest.main()
