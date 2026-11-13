# Temporary Avahi proof guard — code candidate, NOT deployed

This small Python 3.8-standard-library wrapper supervises **one foreground
official Avahi child**. It does not install packages, configure Avahi, create
accounts, start system services, load product code, acquire credentials, or
implement DNS. The earlier `phone-usb-entry` isolation-blocked candidate is
unchanged. This directory is a separate candidate for independent G1 review;
passing local tests is not deployment authorization or a successful phone proof.

## Operator prerequisites (main owns this transaction)

The operator must independently review and provision extracted official
daemon/core-library artifacts and their loader environment in a unique,
root-owned, non-group/other-writable proof directory. There is no dpkg/APT
install transaction in this guard and no host `/etc/avahi` overwrite. The
operator must verify the guard's whole-file SHA in the **outer packaging
manifest**; it is not an extra accepted metadata field.

The operator-owned transient systemd unit must supply the separately reviewed
USB-only Avahi config and an isolated **entire** `/etc/avahi` tree, including
comment-only hosts and empty services, using the root proof tree read-only
bind. Avahi's allow-interface policy must name only the expected interface,
with no default/all-interface fallback. Host name must be the derived name,
not an OS rename. DBus integration and wide-area/reflection must be disabled
as appropriate for this proof; no global DBus reload or system autostart.
The guard pins config bytes but **does not parse or establish the correctness
of Avahi's policy, whole config tree, core libraries or loader environment**.
Those require independent operator/Astra review; a matching file hash does not
make an arbitrary config safe.

The intended outer envelope is DynamicUser with `User/Group=avahi`, no retained
capabilities, NoNewPrivileges, private runtime directory mode 0700,
RuntimeMaxSec=600, Restart=no and KillMode=control-group. The operator must first
prove that NSS resolves Avahi's runtime user/group for the extracted daemon
without creating a permanent account. The guard neither checks nor sets those
unit properties and does not bypass failed NSS/capability checks. No root
daemon is supported: effective uid 0 or nonzero/unreadable CapEff rejects
**before loading metadata or spawning a child**.

## Strict metadata contract

CLI requires explicit `--metadata ABSOLUTE_PATH --duration SECONDS`; duration
must be an integer in **1..570** before any host inspection. The outer unit's
600-second limit remains mandatory. There are no arbitrary command arguments,
shell execution, root/sysfs/identity path switches, daemon mode or PID files.
Import and CLI help perform no guard host/config reads or process launch.

The only JSON keys (all required) are:

- `schema`: integer `1` (not bool).
- `device_id`: `cp_` followed by exactly four ASCII digits.
- `interface`: one legal conservative Linux interface name (1..15 ASCII
  letters/digits/underscore/dot/hyphen; neither `.` nor `..`).
- `selector`: exactly `usb_port`, `vid`, `pid`; a physical USB port such as
  synthetic `4-2.3`, and two lower-case four-digit hex IDs. No serial field.
- `binary_path`: absolute, basename exactly `avahi-daemon`.
- `binary_sha256`: exact lower-case whole-file SHA256.
- `config_path`: absolute, basename exactly `avahi-daemon.conf` (the isolated
  view must be visible to both guard and child).
- `config_sha256`: exact lower-case whole-file SHA256.

No deployed config or template is shipped here; main owns its exact config and
packaging. Unknown keys, duplicate keys (including nested ones), NaN/Infinity,
wrong types, bool-as-int, path traversal and oversized input reject. Metadata
is limited to 16 KiB, binary 8 MiB, config 64 KiB, identity 32 bytes. Metadata,
binary and config and **every path ancestor** must be root-owned, not
group/other-writable and not symlinks. Reads use descriptor-relative nofollow
walking and bounded reads. Root provisioning must keep those paths immutable
while the guard runs; there is no defense against a concurrently malicious
root replacing files after verification.

Identity is read only from `/opt/egorecord/DEVICE_ID`, nofollow including its
ancestors, at startup and once per second. The file may be product-user-owned;
it must be regular and bounded, with only one optional terminal newline.
It must match metadata, deriving e.g. `cp_1234` -> `cp-1234.local` without
special handling for any headring. No phone identity/calibration is copied.

## Runtime behavior and limits

Before the only Popen, require the expected interface to exist, use only
`rndis_host`/`cdc_ncm`, and have a USB ancestor with the exact physical port,
VID and PID. Reject zero/wrong/multiple matching interfaces. Sysfs is consulted
directly; **no `ip` or other periodic subprocess**. Only kernel sysfs links
are followed; test roots are internal dependency injection, never CLI escapes.

The command is fixed:

```
ABSOLUTE_AVAHI_BINARY --no-drop-root --no-chroot --no-proc-title --file=ABSOLUTE_CONFIG
```

It runs as the already nonroot, zero-CapEff guard user. `--no-drop-root` avoids
another user-drop attempt; it does **not** authorize root execution. stdin and
stdout are DEVNULL, stderr PIPE, no shell, inherited descriptors closed. Avahi
is foreground (no `--daemonize`). The outer systemd unit, not this wrapper,
owns the runtime directory and control group.

Every second, privileges, identity and USB qualification are rechecked:

- Expected interface absent: `WAITING`; the standard Avahi interface monitor
  is responsible for withdrawing its own address records.
- Same interface returns with the same selector: `QUALIFIED`; standard Avahi
  is responsible for address/DHCP recovery. The guard pins no IPv4 address.
- Expected interface exists with wrong driver/selector, ambiguity, or changed
  identity: terminate only its own Popen child and exit nonzero.
- Rename to another interface: wait for the original name; **no rename
  adaptation, Family fallback, general scan-and-rebind or process restart**.

Stderr is nonblocking/selectors-based, at most 16 KiB read per loop, with
4096-byte line/pending bounds and <=0.2s selector timeout. It never stores a
raw log history or emits raw child lines. Only derived name/interface plus
fixed state/reason codes are output; cookies, addresses, paths, serials and
credentials in logs are not copied to output. Only matching startup, joining,
and registering events produce a whitelisted Avahi status. Other lines are
discarded, but detected name conflict or nonexpected `Host name is X` fails.
Overflow and child failure also fail closed.

**Important accepted-proof boundary, not a guarantee:** stderr detection is
reactive. Avahi may briefly publish an automatically substituted hostname
**before the guard sees the conflict/wrong-host log and terminates it**. This
guard cannot guarantee that no substitute name packet was ever sent. A new
wrong physical device reusing the interface name, or an identity change, can
also exist before the next one-second check. Namespace/socket policy and
physical-device identity are not made atomic by this wrapper. These windows
must be explicitly accepted by the user and independent reviewer for a
limited proof; otherwise **do not launch it**. No claim of full original
strict zero-transient publication is made.

TERM/INT, errors, duration expiry and constructor/read failures clean up in
finally. Only the owned child is terminated; wait <=2s, then KILL and wait
<=2s. Unreaped/failed cleanup returns nonzero and relies on the outer control
group timeout/cleanup, not unbounded waiting. Signal handlers and selector/
stderr handles are restored/closed. No service stop, pidfile manipulation,
daemonization or other process-killing facility exists.

## Offline tests only

```sh
python3 -m unittest discover -s services/phone-usb-entry-avahi/tests -v
python3 -m py_compile services/phone-usb-entry-avahi/guard.py
```

Tests use fake Popen/clock/readers and synthetic sysfs files. They do not run
the arm64 daemon, inspect a device, bind sockets, query HTTP/ADB, connect to
services or change system state. They cover strict parsing/ownership/paths,
generic identity, root/capabilities and wrong/missing USB zero-spawn paths,
waiting/resume/wrong-device stop, log/name conflict/overflow, fixed argv,
signals, cleanup failures and bounded waits. Python 3.8 AST compatibility on
the local newer interpreter is not actual 3.8/aarch64 validation. Actual
Avahi NSS/nonroot runtime, config isolation, phone mDNS resolution, USB
unplug/DHCP recovery, no-SIM/background/WakeLock behavior remain unverified.
HTTP `.local` does not provide secure context or solve WakeLock/STOP.
