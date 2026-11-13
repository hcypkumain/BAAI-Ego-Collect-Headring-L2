#!/usr/bin/env python3
"""Temporary foreground Avahi guard. Import/help do not inspect the host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

IDENTITY = "/opt/egorecord/DEVICE_ID"
KEYS = {"schema", "device_id", "interface", "selector", "binary_path", "binary_sha256", "config_path", "config_sha256"}


class Refused(Exception):
    pass


def derive(identity):
    if type(identity) is not str or not re.fullmatch(r"cp_[0-9]{4}", identity):
        raise Refused("IDENTITY_INVALID")
    return "cp-" + identity[3:] + ".local"


def absolute(path):
    if (type(path) is not str or not path.startswith("/") or len(path) > 1024
            or any(p in ("", ".", "..") for p in path.split("/")[1:])
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise Refused("PATH_INVALID")
    return path


def secure_read(path, budget, root_owned=True):
    parts = absolute(path).split("/")[1:]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            info = os.fstat(fd)
            if root_owned and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
                raise Refused("UNSAFE_OWNER_OR_MODE")
            nxt = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        info = os.fstat(fd)
        if root_owned and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
            raise Refused("UNSAFE_OWNER_OR_MODE")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_size > budget:
                raise Refused("FILE_TYPE_OR_BUDGET")
            if root_owned and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
                raise Refused("UNSAFE_OWNER_OR_MODE")
            data = bytearray()
            while len(data) <= budget:
                chunk = os.read(source, min(65536, budget + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise Refused("FILE_TYPE_OR_BUDGET")
        finally:
            os.close(source)
    finally:
        os.close(fd)


def metadata(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused("METADATA_INVALID")
            result[key] = value
        return result

    def constant(_value):
        raise Refused("METADATA_INVALID")

    try:
        if len(data) > 16384:
            raise Refused("METADATA_INVALID")
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        if type(value) is not dict or set(value) != KEYS or type(value["schema"]) is not int or value["schema"] != 1:
            raise Refused("METADATA_INVALID")
        derive(value["device_id"])
        if (type(value["interface"]) is not str or value["interface"] in (".", "..")
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value["interface"])):
            raise Refused("METADATA_INVALID")
        selection = value["selector"]
        if type(selection) is not dict or set(selection) != {"usb_port", "vid", "pid"}:
            raise Refused("METADATA_INVALID")
        if type(selection["usb_port"]) is not str or not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*", selection["usb_port"]):
            raise Refused("METADATA_INVALID")
        for key in ("vid", "pid"):
            if type(selection[key]) is not str or not re.fullmatch(r"[0-9a-f]{4}", selection[key]):
                raise Refused("METADATA_INVALID")
        for key, basename in (("binary_path", "avahi-daemon"), ("config_path", "avahi-daemon.conf")):
            if Path(absolute(value[key])).name != basename:
                raise Refused("METADATA_INVALID")
        for key in ("binary_sha256", "config_sha256"):
            if type(value[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
                raise Refused("METADATA_INVALID")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refused("METADATA_INVALID") from None


class Sysfs:
    """Kernel sysfs links are intentional. Alternate root is internal test injection only."""
    def __init__(self, root=Path("/sys/class/net")):
        self.root = root

    def matches(self, name, selection):
        device = (self.root / name / "device").resolve(strict=True)
        driver = (device / "driver").resolve(strict=True).name
        if driver not in ("rndis_host", "cdc_ncm"):
            return False
        for parent in (device,) + tuple(device.parents):
            if parent.name == selection["usb_port"]:
                vid = secure_read(str(parent / "idVendor"), 4096, False).decode("ascii").strip()
                pid = secure_read(str(parent / "idProduct"), 4096, False).decode("ascii").strip()
                return (vid, pid) == (selection["vid"], selection["pid"])
        return False

    def state(self, value):
        expected = value["interface"]
        if not (self.root / expected).exists():
            return "MISSING"
        try:
            if not self.matches(expected, value["selector"]):
                return "WRONG"
            names = list(self.root.iterdir())
            if len(names) > 256:
                return "WRONG"
            matches = 0
            for item in names:
                try:
                    matches += int(self.matches(item.name, value["selector"]))
                except (OSError, Refused, UnicodeError):
                    continue
            return "MATCH" if matches == 1 else "WRONG"
        except (OSError, Refused, UnicodeError):
            return "MISSING" if not (self.root / expected).exists() else "WRONG"


class Host:
    def privileges(self):
        if os.geteuid() == 0:
            raise Refused("ROOT_REFUSED")
        fd = os.open("/proc/self/status", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            status = os.read(fd, 65537)
        finally:
            os.close(fd)
        cap = re.findall(rb"^CapEff:\s*([0-9a-fA-F]{16})$", status, re.M)
        if len(status) > 65536 or len(cap) != 1 or int(cap[0], 16) != 0:
            raise Refused("CAPABILITIES_REFUSED")

    def load(self, path):
        value = metadata(secure_read(path, 16384))
        for prefix, budget in (("binary", 8 * 1024 * 1024), ("config", 65536)):
            if hashlib.sha256(secure_read(value[prefix + "_path"], budget)).hexdigest() != value[prefix + "_sha256"]:
                raise Refused("HASH_MISMATCH")
        return value

    def identity(self):
        value = secure_read(IDENTITY, 32, False).decode("ascii")
        return value[:-1] if value.endswith("\n") else value

    def usb(self, value):
        return Sysfs().state(value)


class LogReader:
    def __init__(self, pipe):
        self.pipe, self.pending = pipe, b""
        self.selector = selectors.DefaultSelector()
        try:
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ)
        except BaseException:
            self.selector.close()
            raise

    def read(self, timeout):
        lines = []
        if self.selector.select(timeout):
            budget = 16384
            while budget:
                try:
                    chunk = os.read(self.pipe.fileno(), min(4096, budget))
                except BlockingIOError:
                    break
                if not chunk:
                    self.selector.unregister(self.pipe)
                    if self.pending:
                        lines.append(self.pending)
                        self.pending = b""
                    break
                budget -= len(chunk)
                parts = (self.pending + chunk).split(b"\n")
                if any(len(part) > 4096 for part in parts):
                    raise Refused("STDERR_OVERFLOW")
                lines.extend(parts[:-1])
                self.pending = parts[-1]
                if b"host name conflict" in self.pending.lower():
                    raise Refused("NAME_CONFLICT")
        return lines

    def close(self):
        self.selector.close()


def log_status(line, value):
    if b"host name conflict" in line.lower():
        raise Refused("NAME_CONFLICT")
    hosts = re.findall(rb"Host name is\s+(\S+)", line, re.I)
    expected = derive(value["device_id"]).encode("ascii")
    if any(host not in (expected, expected + b".") for host in hosts):
        raise Refused("WRONG_HOST_NAME")
    if b"Server startup complete" in line and hosts:
        return "STARTED"
    iface = rb"(?<![A-Za-z0-9_.-])" + re.escape(value["interface"].encode("ascii")) + rb"\.IPv[46](?:[.\s]|$)"
    if re.search(iface, line):
        if b"Joining" in line:
            return "JOINING"
        if b"Registering" in line:
            return "REGISTERING"
    return None


def stop_child(child):
    success = True
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        success = False
    finally:
        try:
            if child.stderr is not None:
                child.stderr.close()
        except OSError:
            success = False
    return success


def run(path, duration, host=None, popen=None, reader_factory=LogReader, clock=time.monotonic, emit=None):
    host, popen = host or Host(), popen or subprocess.Popen
    child = reader = value = None
    result = 2
    interrupted = [False]
    handlers = {}
    def output(state, reason):
        record = {"state": state, "reason": reason}
        if value:
            record.update(name=derive(value["device_id"]), interface=value["interface"])
        if emit:
            emit(record)
        else:
            print(json.dumps(record, sort_keys=True), flush=True)
    try:
        if type(duration) is not int or not 1 <= duration <= 570:
            raise Refused("DURATION_INVALID")
        host.privileges()
        value = host.load(path)
        if host.identity() != value["device_id"]:
            raise Refused("IDENTITY_MISMATCH")
        if host.usb(value) != "MATCH":
            raise Refused("USB_STARTUP_REFUSED")
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, lambda _sig, _frame: interrupted.__setitem__(0, True))
        if interrupted[0]:
            raise Refused("INTERRUPTED")
        deadline = clock() + duration
        child = popen([value["binary_path"], "--no-drop-root", "--no-chroot", "--no-proc-title",
                       "--file=" + value["config_path"]], stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, shell=False, close_fds=True)
        reader = reader_factory(child.stderr)
        next_check, previous = clock(), None
        while clock() < deadline:
            if interrupted[0]:
                raise Refused("INTERRUPTED")
            if child.poll() is not None:
                raise Refused("CHILD_EXITED")
            now = clock()
            if now >= next_check:
                host.privileges()
                if host.identity() != value["device_id"]:
                    raise Refused("IDENTITY_CHANGED")
                state = host.usb(value)
                if state not in ("MISSING", "MATCH"):
                    raise Refused("USB_CHANGED")
                if state != previous:
                    output("WAITING" if state == "MISSING" else "QUALIFIED", "USB_ABSENT" if state == "MISSING" else "USB_MATCH")
                previous, next_check = state, now + 1
            for line in reader.read(max(0, min(0.2, next_check - clock(), deadline - clock()))):
                status = log_status(line, value)
                if status:
                    output(status, "AVAHI_SAFE_STATUS")
        output("STOPPED", "DURATION_COMPLETE")
        result = 0
    except (Refused, OSError, UnicodeError, ValueError) as exc:
        # Never expose raw path, child output, identity, cookie, or exceptions.
        output("ERROR", str(exc) if isinstance(exc, Refused) else "GUARD_IO_ERROR")
    finally:
        try:
            if child is not None and not stop_child(child):
                output("ERROR", "CLEANUP_FAILED")
                result = 2
        finally:
            try:
                if reader is not None:
                    reader.close()
            except OSError:
                output("ERROR", "CLEANUP_FAILED")
                result = 2
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--duration", required=True, type=int, help="1..570 seconds; enforced before host reads")
    args = parser.parse_args(argv)
    return run(args.metadata, args.duration)


if __name__ == "__main__":
    raise SystemExit(main())
