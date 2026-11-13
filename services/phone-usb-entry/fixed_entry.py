#!/usr/bin/env python3
"""Offline-only, fail-closed fixed-entry candidate. Never opens a socket.

Snapshot input is synthetic/untrusted evidence, NOT live interface attestation.
The known library isolation blocker cannot be overridden by configuration.
"""
import argparse
import ipaddress
import json
import os
import re
import stat

BLOCKER = "ISOLATION_UNAVAILABLE"
MAX_JSON = 65536


class Invalid(ValueError):
    """Intentionally opaque: do not include private config contents in errors."""


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid("duplicate-key")
            result[key] = value
        return result

    def constant(_value):
        raise Invalid("nonfinite-number")

    if len(data) > MAX_JSON:
        raise Invalid("input-budget")
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise Invalid("invalid-json") from None


def private_read(path, limit=MAX_JSON):
    """Walk with openat + O_NOFOLLOW, including every ancestor (Linux).

    Root-owned ancestors such as /home are allowed, but the immediate private
    directory and file must belong to this uid and have exact 0700/0600 modes.
    No config keys, serials or file contents are exposed in diagnostics.
    """
    if ".." in os.fspath(path).split(os.sep):
        raise Invalid("private-path-refused")
    parts = os.path.abspath(path).split(os.sep)[1:]
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        parent = os.fstat(fd)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise Invalid("private-parent-permissions")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(source)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Invalid("private-file-permissions")
            if info.st_size > limit:
                raise Invalid("input-budget")
            data = os.read(source, limit + 1)
            if len(data) > limit:
                raise Invalid("input-budget")
            return data
        finally:
            os.close(source)
    except OSError:
        raise Invalid("private-path-refused") from None
    finally:
        os.close(fd)


def object_keys(value, required, optional=()):
    if type(value) is not dict or not set(required) <= set(value) <= set(required) | set(optional):
        raise Invalid("object-schema")


def selector(value):
    object_keys(value, ("usb_port", "vid", "pid"), ("serial",))
    if not isinstance(value["usb_port"], str) or not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*", value["usb_port"]):
        raise Invalid("usb-port")
    for key in ("vid", "pid"):
        if type(value[key]) is not str or not re.fullmatch(r"[0-9a-f]{4}", value[key]):
            raise Invalid("usb-id")
    if "serial" in value and (type(value["serial"]) is not str or not 1 <= len(value["serial"]) <= 128
                              or not value["serial"].isprintable()):
        raise Invalid("usb-serial")
    return dict(value)


def hostname(device_id):
    if type(device_id) is not str or not re.fullmatch(r"cp_[0-9]{4}", device_id):
        raise Invalid("device-id")
    return "cp-" + device_id[3:] + ".local."


def qualify(selection, snapshot):
    """Pure join of explicit USB/sysfs evidence with ip -j-shaped evidence.

    Never scans the host. Production sysfs/ip acquisition is intentionally
    absent until the network isolation gate has a reviewed solution.
    """
    selection = selector(selection)
    object_keys(snapshot, ("usb", "ip"))
    if type(snapshot["usb"]) is not list or type(snapshot["ip"]) is not list:
        raise Invalid("snapshot-schema")
    if len(snapshot["usb"]) > 32 or len(snapshot["ip"]) > 64:
        raise Invalid("snapshot-budget")
    matches = []
    names = set()
    for usb in snapshot["usb"]:
        object_keys(usb, ("usb_port", "vid", "pid", "driver", "interface", "ifindex", "generation", "carrier"), ("serial",))
        if type(usb["interface"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", usb["interface"]):
            raise Invalid("interface")
        if usb["interface"] in names:
            raise Invalid("duplicate-interface")
        names.add(usb["interface"])
        if any(type(usb[k]) is not int or usb[k] < 1 for k in ("ifindex", "generation")) or type(usb["carrier"]) is not bool:
            raise Invalid("link-schema")
        if all(usb.get(k) == v for k, v in selection.items()):
            matches.append(usb)
    if len(matches) != 1:
        raise Invalid("usb-zero-or-multiple")
    usb = matches[0]
    if usb["driver"] not in ("rndis_host", "cdc_ncm"):
        raise Invalid("usb-driver")
    if not usb["carrier"]:
        raise Invalid("link-down")
    rows = [r for r in snapshot["ip"] if type(r) is dict and r.get("ifname") == usb["interface"]]
    if len(rows) != 1 or rows[0].get("ifindex") != usb["ifindex"] or type(rows[0].get("ifindex")) is not int:
        raise Invalid("ip-interface-mismatch")
    row = rows[0]
    if type(row.get("flags")) is not list or "UP" not in row["flags"] or "LOWER_UP" not in row["flags"]:
        raise Invalid("link-down")
    if type(row.get("addr_info")) is not list or len(row["addr_info"]) > 32:
        raise Invalid("address-budget")
    addresses = []
    for info in row["addr_info"]:
        if type(info) is not dict:
            raise Invalid("address-schema")
        if info.get("family") != "inet" or info.get("scope") != "global":
            continue
        try:
            addr = ipaddress.IPv4Address(info["local"])
            if type(info["local"]) is not str or addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_link_local or addr.is_reserved:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise Invalid("ipv4") from None
        if info.get("tentative", False) or info.get("dadfailed", False) or info.get("deprecated", False) or info.get("valid_life_time") == 0:
            raise Invalid("address-not-ready")
        addresses.append(str(addr))
    if len(addresses) != 1:
        raise Invalid("ipv4-zero-or-multiple")
    return {"interface": usb["interface"], "IP": addresses[0], "generation": usb["generation"],
            "ifindex": usb["ifindex"]}


def lifecycle_plan(previous, current):
    """Offline policy model, NOT an implemented publisher or live reconciler.

    The caller must first turn missing/ambiguous/down snapshots into None.
    Address, rename, ifindex or generation changes always revoke the old
    generation before binding the next one. No update-in-place is permitted.
    """
    if previous == current:
        return []
    actions = ["unregister-old", "close-old"] if previous is not None else []
    if current is not None:
        actions += ["bind-explicit-ipv4-only", "register-no-name-change"]
    return actions


def require_isolation():
    # No config switch, duration, constructor interface list, or operator flag
    # can establish the missing receive/unicast-egress enforcement.
    raise Invalid(BLOCKER)


def service_info(name, qualified):
    """Construct real library RR data only: no Zeroconf, registration or sockets."""
    from zeroconf import ServiceInfo
    if not re.fullmatch(r"cp-[0-9]{4}\.local\.", name):
        raise Invalid("host-name")
    return ServiceInfo("_http._tcp.local.", name[:-7] + "._http._tcp.local.",
                       server=name, port=8765, properties={"path": "/phone"},
                       addresses=[ipaddress.IPv4Address(qualified["IP"]).packed],
                       host_ttl=30, other_ttl=30)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline synthetic check only; always blocked for publication")
    parser.add_argument("--publish", action="store_true", help="refused before input reads or any network activity")
    parser.add_argument("--duration", type=int, help="requested bounded session, 1..600 seconds; does not bypass isolation")
    parser.add_argument("--selector", help="explicit private strict JSON file")
    parser.add_argument("--device-id", help="explicit private identity file; cp_NNNN")
    parser.add_argument("--snapshot", help="explicit private synthetic snapshot JSON")
    args = parser.parse_args(argv)
    result = {"name": None, "interface": None, "IP": None, "generation": None,
              "state": "blocked", "reason": BLOCKER}
    if args.publish:
        # Deliberately before file I/O, dependency imports, publisher creation,
        # subprocesses, PID/status logs or network checks (including port 5353).
        try:
            require_isolation()
        except Invalid:
            pass
    elif args.check:
        try:
            if not all((args.selector, args.device_id, args.snapshot)):
                raise Invalid("explicit-inputs-required")
            identity = private_read(args.device_id, 32).decode("ascii")
            name = hostname(identity[:-1] if identity.endswith("\n") else identity)
            candidate = qualify(strict_json(private_read(args.selector)), strict_json(private_read(args.snapshot)))
            result.update({k: candidate[k] for k in ("interface", "IP", "generation")}, name=name)
        except (Invalid, UnicodeError) as exc:
            result["reason"] = str(exc) if isinstance(exc, Invalid) else "device-id"
    print(json.dumps(result, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
