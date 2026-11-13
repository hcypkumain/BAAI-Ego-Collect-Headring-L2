"""All process/Avahi tests use synthetic fixtures and fake Popen; no LAN."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "guard.py"
SPEC = importlib.util.spec_from_file_location("avahi_proof_guard", SOURCE)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def metadata():
    return {"schema": 1, "device_id": "cp_1234", "interface": "usb9",
            "selector": {"usb_port": "4-2.3", "vid": "1234", "pid": "abcd"},
            "binary_path": "/opt/synthetic/usr/sbin/avahi-daemon", "binary_sha256": hashlib.sha256(b"binary").hexdigest(),
            "config_path": "/etc/avahi/avahi-daemon.conf", "config_sha256": hashlib.sha256(b"config").hexdigest()}


class Clock:
    now = 0.0
    def __call__(self):
        return self.now


class Child:
    def __init__(self):
        self.stderr = io.BytesIO()
        self.terminate = Mock()
        self.kill = Mock()
        self.wait = Mock(return_value=0)
        self.poll = Mock(return_value=None)


class Host:
    def __init__(self):
        self.privileges = Mock()
        self.load = Mock(return_value=metadata())
        self.identity = Mock(return_value="cp_1234")
        self.usb = Mock(return_value="MATCH")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.host, self.child, self.clock = Host(), Child(), Clock()
        self.spawn, self.records = Mock(return_value=self.child), []
        self.lines, self.closed = [], False
        owner = self
        class Reader:
            def __init__(self, _pipe):
                pass
            def read(self, timeout):
                owner.clock.now += max(timeout, 0.001)
                lines, owner.lines = owner.lines, []
                return lines
            def close(self):
                owner.closed = True
        self.reader = Reader
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Any accidental real Popen, socket, or fork fails these tests.
        self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("no sockets")))
        self.stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no real child")))
        self.stack.enter_context(patch("os.fork", side_effect=AssertionError("no fork")))

    def run_guard(self, duration=4):
        return guard.run("/synthetic/metadata.json", duration, host=self.host, popen=self.spawn,
                         reader_factory=self.reader, clock=self.clock, emit=self.records.append)

    def test_generic_identity_and_py38_syntax(self):
        for digits in ("0000", "0021", "1234", "9999"):
            self.assertEqual(guard.derive("cp_" + digits), "cp-" + digits + ".local")
        for value in ("cp_21", "cp_１２３４", "cp_1234\n", True):
            with self.assertRaises(guard.Refused):
                guard.derive(value)
        ast.parse(SOURCE.read_text(), feature_version=(3, 8))

    def test_import_and_help_do_not_inspect_host(self):
        text = SOURCE.read_text()
        with patch("os.open", side_effect=AssertionError("no IO")), patch("builtins.open", side_effect=AssertionError("no IO")), \
                patch.object(guard.Host, "privileges", side_effect=AssertionError("no inspect")):
            namespace = {"__name__": "not_main"}
            exec(compile(text, str(SOURCE), "exec"), namespace)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                namespace["main"](["--help"])
            self.assertEqual(caught.exception.code, 0)
    def test_metadata_strict(self):
        self.assertEqual(guard.metadata(json.dumps(metadata())), metadata())
        for raw in ('{"schema":1,"schema":1}', '{"x":NaN}', '{"x":Infinity}', "[]", " " * 16385):
            with self.assertRaises(guard.Refused):
                guard.metadata(raw)
        for changes in ({"schema": True}, {"device_id": 21}, {"interface": "usb9;sh"},
                        {"binary_path": "/opt/synthetic/other"}, {"binary_path": "/opt/../avahi-daemon"},
                        {"config_sha256": False}, {"extra": "SECRET"}, {"guard_sha256": "0" * 64},
                        {"selector": {"usb_port": "4-2.3", "vid": True, "pid": "abcd"}}):
            with self.subTest(changes=changes), self.assertRaises(guard.Refused):
                guard.metadata(json.dumps(dict(metadata(), **changes)))

    def test_root_and_caps_refuse_zero_spawn(self):
        for uid, content, reason in ((0, b"", "ROOT_REFUSED"), (1001, b"CapEff:\t0000000000000001\n", "CAPABILITIES_REFUSED"),
                                     (1001, b"CapEff:\tUNKNOWN\n", "CAPABILITIES_REFUSED")):
            with patch("os.geteuid", return_value=uid), patch("os.open", return_value=100), \
                    patch("os.read", return_value=content), patch("os.close"):
                self.host.privileges = guard.Host().privileges
                self.assertEqual(self.run_guard(), 2)
                self.assertEqual(self.records[-1]["reason"], reason)
        self.spawn.assert_not_called()
        self.host.load.assert_not_called()

    def test_caps_zero_allowed_and_bad_duration_zero_io(self):
        with patch("os.geteuid", return_value=1001), patch("os.open", return_value=100), \
                patch("os.read", return_value=b"CapEff:\t0000000000000000\n"), patch("os.close"):
            guard.Host().privileges()
        for duration in (0, 571, True, "3"):
            self.assertEqual(self.run_guard(duration), 2)
        self.host.privileges.assert_not_called()
        self.spawn.assert_not_called()

    def test_missing_wrong_and_identity_mismatch_zero_spawn(self):
        for state in ("MISSING", "WRONG"):
            self.host.usb.return_value = state
            self.assertEqual(self.run_guard(), 2)
        self.host.identity.return_value = "cp_9999"
        self.assertEqual(self.run_guard(), 2)
        self.spawn.assert_not_called()

    def test_disconnect_wait_resume_and_wrong_device_stop(self):
        self.host.usb.side_effect = ["MATCH", "MATCH", "MISSING", "MATCH", "WRONG"]
        self.assertEqual(self.run_guard(5), 2)
        self.assertIn("WAITING", [r["state"] for r in self.records])
        self.assertEqual(sum(r["state"] == "QUALIFIED" for r in self.records), 2)
        self.assertEqual(self.records[-1]["reason"], "USB_CHANGED")
        self.child.terminate.assert_called_once()
        self.assertEqual(self.spawn.call_count, 1)

    def test_identity_change_stops_own_child(self):
        self.host.identity.side_effect = ["cp_1234", "cp_1234", "cp_9999"]
        self.assertEqual(self.run_guard(), 2)
        self.assertEqual(self.records[-1]["reason"], "IDENTITY_CHANGED")
        self.child.terminate.assert_called_once()

    def test_conflict_wrong_host_and_safe_status(self):
        for line, reason in ((b"Host name conflict, retrying with SECRET", "NAME_CONFLICT"),
                             (b"Server startup complete. Host name is cp-1234-2.local. cookie=SECRET", "WRONG_HOST_NAME")):
            with self.assertRaises(guard.Refused) as caught:
                guard.log_status(line, metadata())
            self.assertEqual(str(caught.exception), reason)
        self.assertEqual(guard.log_status(b"Server startup complete. Host name is cp-1234.local. Local service cookie is SECRET", metadata()), "STARTED")
        self.assertEqual(guard.log_status(b"Joining mDNS multicast group on interface usb9.IPv4 with address SECRET", metadata()), "JOINING")
        self.assertEqual(guard.log_status(b"Registering new address record for SECRET on usb9.IPv4.", metadata()), "REGISTERING")
        self.assertIsNone(guard.log_status(b"Joining on usb99.IPv4 SECRET", metadata()))
        self.lines = [b"Host name conflict SECRET"]
        self.assertEqual(self.run_guard(), 2)
        self.child.terminate.assert_called_once()
        self.assertNotIn("SECRET", json.dumps(self.records))

    def test_fixed_argv_duration_cleanup_and_wait_budget(self):
        self.child.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 2), 0]
        self.assertEqual(self.run_guard(1), 0)
        args, kwargs = self.spawn.call_args
        self.assertEqual(args[0], [metadata()["binary_path"], "--no-drop-root", "--no-chroot", "--no-proc-title", "--file=" + metadata()["config_path"]])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.child.kill.assert_called_once()
        self.assertEqual([c.kwargs for c in self.child.wait.call_args_list], [{"timeout": 2}, {"timeout": 2}])
        self.assertTrue(self.child.stderr.closed and self.closed)

    def test_wrong_host_always_stops_and_redacts(self):
        self.lines = [b"Server startup complete. Host name is cp-1234-2.local. cookie=SECRET"]
        self.assertEqual(self.run_guard(), 2)
        self.assertEqual(self.records[-1]["reason"], "WRONG_HOST_NAME")
        self.child.terminate.assert_called_once()
        self.assertNotIn("SECRET", json.dumps(self.records))

    def test_overflow_cleanup_and_failed_kill_wait_are_bounded(self):
        self.reader = Mock(side_effect=guard.Refused("STDERR_OVERFLOW"))
        self.child.wait.side_effect = subprocess.TimeoutExpired("synthetic", 2)
        self.assertEqual(self.run_guard(), 2)
        self.assertEqual(self.records[-1]["reason"], "CLEANUP_FAILED")
        self.assertIn("STDERR_OVERFLOW", [r["reason"] for r in self.records])
        self.child.terminate.assert_called_once()
        self.child.kill.assert_called_once()
        self.assertEqual(self.child.wait.call_count, 2)
        self.assertTrue(self.child.stderr.closed)

    def test_reader_failure_cleanup(self):
        self.reader = Mock(side_effect=OSError("SECRET"))
        self.assertEqual(self.run_guard(), 2)
        self.assertTrue(self.child.stderr.closed)
        self.child.terminate.assert_called_once()
        self.assertNotIn("SECRET", json.dumps(self.records))

    def test_signal_cleanup(self):
        # Test handler delivery without sending a real process signal.
        original_handler = signal.getsignal(signal.SIGTERM)
        original_reader = self.reader
        def factory(pipe):
            reader = original_reader(pipe)
            original_read = reader.read
            def read(timeout):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return original_read(timeout)
            reader.read = read
            return reader
        self.reader = factory
        self.assertEqual(self.run_guard(), 2)
        self.assertEqual(self.records[-1]["reason"], "INTERRUPTED")
        self.child.terminate.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)

    def test_sigint_cleanup(self):
        original_handler = signal.getsignal(signal.SIGINT)
        original_reader = self.reader
        def factory(pipe):
            reader = original_reader(pipe)
            original_read = reader.read
            def read(timeout):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                return original_read(timeout)
            reader.read = read
            return reader
        self.reader = factory
        self.assertEqual(self.run_guard(), 2)
        self.assertEqual(self.records[-1]["reason"], "INTERRUPTED")
        self.child.terminate.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)

    def test_log_reader_overflow_and_loop_budget(self):
        pipe = Mock()
        pipe.fileno.return_value = 100
        selector = Mock()
        selector.select.return_value = [(None, 1)]
        with patch("selectors.DefaultSelector", return_value=selector), patch("os.set_blocking"), \
                patch("os.read", side_effect=[b"x" * 4096, b"y"]):
            reader = guard.LogReader(pipe)
            with self.assertRaises(guard.Refused) as caught:
                reader.read(0.2)
            self.assertEqual(str(caught.exception), "STDERR_OVERFLOW")
            reader.close()
        with patch("selectors.DefaultSelector", return_value=selector), patch("os.set_blocking"), \
                patch("os.read", return_value=b"x\n" * 2048) as read:
            reader = guard.LogReader(pipe)
            reader.read(0.2)
            self.assertEqual(read.call_count, 4)
            reader.close()
        selector.close.assert_called()


class FileAndSysfsTests(unittest.TestCase):
    def test_nofollow_paths_and_root_owner(self):
        with tempfile.TemporaryDirectory(prefix="8788-avahi-", dir="/tmp/opencode") as root:
            root = Path(root)
            (root / "identity").write_bytes(b"cp_1234\n")
            self.assertEqual(guard.secure_read(str(root / "identity"), 32, False), b"cp_1234\n")
            (root / "link").symlink_to(root / "identity")
            (root / "dirlink").symlink_to(root, target_is_directory=True)
            for path in (root / "link", root / "dirlink" / "identity"):
                with self.assertRaises(OSError):
                    guard.secure_read(str(path), 32, False)
            with self.assertRaises(guard.Refused):
                guard.secure_read(str(root / "identity"), 2, False)
            with patch("os.fstat", return_value=SimpleNamespace(st_uid=77, st_mode=0o40755)), self.assertRaises(guard.Refused):
                guard.secure_read(str(root / "identity"), 32)
            with patch("os.fstat", return_value=SimpleNamespace(st_uid=0, st_mode=0o40777)), self.assertRaises(guard.Refused):
                guard.secure_read(str(root / "identity"), 32)

    def test_hash_validation(self):
        def read(path, _budget):
            return {"/synthetic/meta": json.dumps(metadata()).encode(), metadata()["binary_path"]: b"binary", metadata()["config_path"]: b"config"}[path]
        with patch.object(guard, "secure_read", side_effect=read):
            self.assertEqual(guard.Host().load("/synthetic/meta"), metadata())
        with patch.object(guard, "secure_read", side_effect=[json.dumps(metadata()).encode(), b"wrong"]), self.assertRaises(guard.Refused):
            guard.Host().load("/synthetic/meta")

    def test_real_synthetic_sysfs_selector_unique_missing_rename_wrong_driver(self):
        with tempfile.TemporaryDirectory(prefix="8788-sysfs-", dir="/tmp/opencode") as temp:
            root = Path(temp)
            net = root / "net"
            net.mkdir()
            usb = root / "4-2.3"
            usb.mkdir()
            (usb / "idVendor").write_text("1234\n")
            (usb / "idProduct").write_text("abcd\n")
            driver = root / "rndis_host"
            driver.mkdir()
            device = usb / "4-2.3:1.0"
            device.mkdir()
            (device / "driver").symlink_to(driver, target_is_directory=True)
            iface = net / "usb9"
            iface.mkdir()
            (iface / "device").symlink_to(device, target_is_directory=True)
            sysfs = guard.Sysfs(net)
            self.assertEqual(sysfs.state(metadata()), "MATCH")
            iface.rename(net / "usb10")
            self.assertEqual(sysfs.state(metadata()), "MISSING")
            (net / "usb10").rename(iface)
            self.assertEqual(sysfs.state(metadata()), "MATCH")
            other = net / "usb11"
            other.mkdir()
            (other / "device").symlink_to(device, target_is_directory=True)
            self.assertEqual(sysfs.state(metadata()), "WRONG")
            (other / "device").unlink()
            self.assertEqual(sysfs.state(metadata()), "MATCH")
            (usb / "idProduct").write_text("ffff\n")
            self.assertEqual(sysfs.state(metadata()), "WRONG")
            (usb / "idProduct").write_text("abcd\n")
            (device / "driver").unlink()
            wifi = root / "iwlwifi"
            wifi.mkdir()
            (device / "driver").symlink_to(wifi, target_is_directory=True)
            self.assertEqual(sysfs.state(metadata()), "WRONG")


if __name__ == "__main__":
    unittest.main()
