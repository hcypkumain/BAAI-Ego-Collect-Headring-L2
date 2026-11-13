# USB fixed entry — code candidate, isolation blocked, NOT deployed

This is a temporary **offline review candidate**, not a working mDNS service or
a production delivery. Default execution and `--publish` return exit 2 with
`state=blocked`, `reason=ISOLATION_UNAVAILABLE`. This gate runs before input
reads, dependency imports, socket creation, subprocesses, port claims or
publication. There is no override, daemon, autostart, root operation or network
fallback. `--duration` (even 600) cannot bypass the gate. Permanent sessions are
unsupported. No LAN/mDNS process is launched by these files or their tests.

## Why publishing is blocked

Official python-zeroconf 0.136.2 exposes `interfaces=[USB_IP]` and
`IPVersion.V4Only`, but those are **not strict Linux USB isolation**:

* `_utils/net.py:create_sockets` creates a `("", 5353)` receive socket even
  with an explicit IPv4 interface. `new_socket` enables address/port reuse.
* It sets neither Linux `IP_MULTICAST_ALL=0` nor device-bound receive/egress.
  Linux defaults `IP_MULTICAST_ALL` to 1. A group's membership on another
  interface can therefore affect wildcard reception; the constructor argument
  alone does not prove isolation. Ordinary unicast wildcard reception also
  lacks a receiving-interface guard.
* `_engine.py:_async_create_endpoints` makes that wildcard socket a reader.
  `_listener.py` passes its transport to `QueryHandler`; the query handler's
  `handle_assembled_query` passes the incoming transport to `zc.async_send`
  for unicast replies. `_core.py:async_send_with_transport` calls `sendto` on
  that transport without an interface/destination-policy guard.
* USB source binding plus `IP_MULTICAST_IF` on the separate respond socket
  controls multicast selection, not all unicast routing paths.

The tests call the **unmodified library** socket-creation functions with a
recording socket double and its real query/packet/send code with mock
transports. They demonstrate wildcard construction and acceptance of an
off-USB unicast destination. No real packets are sent, and these are not a
physical multi-interface packet capture. No mature public API furnishing the
required complete restriction was found in this pinned version. No private
socket monkeypatch, home-grown DNS codec, capability, sysctl or firewall
workaround is included. A separately reviewed interface-restricted OS service
is outside this candidate and requires separate authorization.

## What exists, and what does not

Implemented: strict private JSON reads (duplicate/nonfinite rejection,
schema/type/budget checks, no symlink ancestors, uid and 0700/0600 checks),
generic `cp_NNNN` -> `cp-NNNN.local.` derivation, a pure synthetic USB/IP
qualification function, an **offline lifecycle policy model**, mature-library
ServiceInfo construction and direct A/AAAA query/packet tests, locked offline
dependency builder. The service RR uses `_http._tcp.local.`, port 8765,
TXT `path=/phone`, and only the single qualified IPv4. AAAA is absent (NSEC),
not invented from another interface. DNS encoding/decoding is entirely upstream.

**Not implemented or validated:** live sysfs and `ip -j` acquisition/attestation,
live address watcher, actual registration or collision handling
(`allow_name_change=False` would be mandatory), USB-only socket isolation,
exclusive 5353 claim/race protection, bounded 600-second session, TERM/INT
unregister/close, thread cleanup, private PID/starttime/status-log IPC. These
requirements must not be represented as tested runtime features. The offline
model requires unregister-old/close-old before rebinding on address, interface,
ifindex or generation changes; it performs no such operation on a device.
The blocked CLI has no loop, background thread, fork, PID file or socket, so
it has nothing to unregister or leave behind. No disabled skeleton publisher
is shipped as a purported implementation.

## Offline synthetic check

All inputs must be explicit files in an operator-owned 0700 directory, regular
0600 single-link files with no symlink in the path. Never use active product
config, control credentials or real serials for these examples. The helper
treats file reads as opaque and never prints config keys, contents or serials.
There are no default device paths and no special handling for one headring.

Synthetic selector JSON:

```json
{"usb_port":"4-2.3","vid":"1234","pid":"abcd"}
```

Synthetic identity file: `cp_1234` (one optional terminal newline).
Synthetic snapshot JSON (not trustworthy live evidence):

```json
{"usb":[{"usb_port":"4-2.3","vid":"1234","pid":"abcd","driver":"rndis_host","interface":"usb9","ifindex":12,"generation":1,"carrier":true}],"ip":[{"ifname":"usb9","ifindex":12,"flags":["UP","LOWER_UP"],"addr_info":[{"family":"inet","scope":"global","local":"192.0.2.10"}]}]}
```

```sh
python3 services/phone-usb-entry/fixed_entry.py --check \
  --selector /explicit/private/selector.json \
  --device-id /explicit/private/DEVICE_ID \
  --snapshot /explicit/private/synthetic.json
```

Output contains only name/interface/IP/generation/state/reason; even a valid
check exits 2 and remains isolation-blocked. Zero/multiple matching USB devices,
non-rndis_host/non-cdc_ncm drivers, wrong IDs/serial, link down, missing or
multiple global IPv4s, or ip/sysfs index mismatch reject the candidate. Caller
snapshots are not a defense against forged interface ancestry; no live readiness
claim follows from passing a synthetic check.

## Offline dependency closure and payload

`requirements.lock.json` pins official PyPI URLs, full archive SHA256 and sizes.
Supply those exact original archives in an **external** `--vendor-root`.
The builder does not download, resolve dependencies or install anything. It
requires a full immutable Git commit SHA and a new operator-owned staging
directory, reads only this new service's three files from Git, then reads
whitelisted package `.py` members and complete upstream licenses. It never
executes setup/build hooks, Cython, compilers or upstream tests. Archive paths,
types, duplicates, sizes and hashes are checked. No dependency vendor is in Git.

```sh
python3 scripts/build_phone_usb_entry.py \
  --repo /path/to/this/worktree --ref FULL_40_HEX_COMMIT \
  --vendor-root /explicit/external/official-archives \
  --staging /explicit/new-user-staging
```

The reproducible tar has normalized mode/uid/gid/mtime, a per-file hash/size
manifest, Git ref and lock hash; outer `SHA256SUMS` covers the payload. No
private config, original product source, service unit, installer or deployment
script is included. This is a **review artifact**, not installation permission.
Inspect/extract only into a new private user staging directory; do not install
in site-packages or overlay the existing product.

After a safe whitelist extraction of the payload, offline tests can use:

```sh
PHONE_USB_ENTRY_VENDOR=/explicit/staging/python \
  python3 -m unittest discover -s services/phone-usb-entry/tests -v
```

Without that environment variable dependency tests explicitly skip; skipped
tests are not evidence of a validated closure. Tests otherwise need only the
standard library. Test network functions use recording mocks, not LAN sockets.
Fresh-process audit probes (first one permits zero child processes):

```sh
python3 -I -S -B services/phone-usb-entry/tests/offline_probe.py blocked \
  services/phone-usb-entry/fixed_entry.py
python3 -I -S -B services/phone-usb-entry/tests/offline_probe.py imports \
  /explicit/staging/python
```

Pure Python imports, RR serialization and Python 3.8 syntax can be checked on
a newer interpreter, but **actual 3.8/aarch64 runtime is not proven by that**.
On any later authorized device validation, first run isolated import/check-only
with explicit exports; never launch networking as an import smoke test.

Python 3.8 and this pinned older stack are EOL/old dependencies. They are only
a temporary proof baseline, not a long-term security guarantee. Cython is
optional upstream; the selected Python closure imports/runs without it.
ifaddr uses system libc via ctypes on Linux, not a bundled compiler artifact.
**Import is not guaranteed fork-free:** its import-time
`ctypes.util.find_library("c")` tries a read-only `ldconfig -p` subprocess on
this Linux host; stdlib fallbacks can also invoke other inspection/compiler
tools if lookup fails. An isolated audit denying all forks correctly blocked
that import. A separate local smoke test permits only the exact read-only
`ldconfig -p` command and denies compiler/other subprocesses and all sockets.
Do not monkeypatch the library or assume this import behavior is acceptable
on a target device. The CLI's isolation-blocked path imports none of these
dependencies and is independently tested to create zero sockets/forks.

## User-facing limitations

Intended bookmark would be `http://cp-NNNN.local:8765/phone`, not a hostname
change or generic `headring.local`. **This candidate does not make it work.**
Android USB mDNS resolution, real unplug/replug, DHCP address recovery,
no-SIM operation, background behavior and WakeLock remain unverified. Ordinary
HTTP `.local` does not supply a secure context, solve WakeLock or change STOP
semantics. No Family/Guest routes, DHCP, DNS, default route, ADB, browser,
authentication, recording, overlay, START or impedance behavior is changed.
