# Wired phone USB tether transport (offline candidate)

**Implemented and offline-tested; not installed, not device-accepted, not approved for production.**
This branch starts at a WIP backup, not a production release. Do not deploy its
unrelated START/impedance/scan/task changes. Independent review and a separately
approved idle-device deployment/acceptance window remain mandatory.

## Boundary

Android USB **device**, manually enabled USB tether (RNDIS or NCM) → existing Pi
USB **data host** → qualified usbnet interface → existing HTTP `8765/phone`.
Reference images, preview and control use the same wired HTTP origin; no Wi-Fi
replacement, new HTTP port, proxy protocol, phone application, ADB command,
automatic phone mode switch, or runtime fallback was added. Existing ADB and
`headring-usb-data-host` policy/protocol are unchanged.

The observer only reads sysfs and `ip -j address show`. It does not request DHCP,
ping, scan, probe peers, install drivers, set routes, or change USB/network/services.
DHCP/network provisioning is a separate operator-reviewed prerequisite. Driver
support and a Pi IP **do not identify a phone, prove connectivity, register its
display, or permit START**. Existing authentication, display registration, black
ACK, privacy lock/unlock and session-bound STOP remain authoritative and unchanged.

## Configuration and discovery

`/etc/headring-phone-transport.json` defaults to `adb` when absent. Illegal modes,
unknown keys and invalid selectors are errors, never fallback. Example shipped in
`deployment/phone-tether/transport.example.json` intentionally stays `adb`.
Only after reviewing the actual device, prepare a private configuration such as:

```json
{"mode":"usb_tether","selector":{"usb_port":"1-2.3","vid":"18d1","pid":"4ee3"}}
```

Values above are illustrative, **not a supported device declaration**. `serial`
is optional and sensitive. The required physical USB port and VID/PID must match
the sysfs USB ancestor; interface driver must be exactly `rndis_host` or `cdc_ncm`.
Ordinary Ethernet, Family WLAN, wrong devices and broad driver matching are not
phone candidates. Multiple qualified interfaces are ambiguous: no addresses are
published. Interface names/MACs/Android subnets are not assumed. Missing carrier or
usable private IPv4 is `no_address`. Re-enumeration/rename/unplug invalidates old
addresses; consumer rechecks live topology and IPs even within cache TTL.

Observer CLI (read status does not write):

```sh
python3 services/phone-tether/tether_link.py status --config /path/reviewed.json
python3 services/phone-tether/tether_link.py --once --config /path/reviewed.json --cache /path/private-runtime/link.json
python3 services/phone-tether/tether_link.py monitor --config /path/reviewed.json --cache /path/private-runtime/link.json
```

These commands are for a later approved environment, not instructions to run on a
production device now. Writer refuses UID 0; cache parent must already exist,
be owned by writer, private `0700`, and symlink-free. Supplied unit uses `orangepi`,
private RuntimeDirectory and UMask `0077`, no capabilities/NET_ADMIN. The dashboard
consumer must run as the **same UID**, or adapt the reviewed service account in
both places. Config target ownership is `root:orangepi 0640` (consumer readable,
not group-writable). No secrets are printed in public status or API errors.

### Cache schema 1 (private internal file)

Fields: `schema:1`, `boot_id`, `monotonic`, `observer_instance` (UUID), `mode`,
`selector` (private, potentially serial), `state`, `reason`, `links` (kind,
interface, address). Atomic replace, `0600`. Consumer requires matching config,
boot, valid instance, age `0..5` monotonic seconds, safe owner/file permissions,
cache file and parent owned by consumer UID with no group/other access,
regular bounded JSON (32 KiB), no symlink path, validated IPv4/interfaces and
matching current topology/addresses. Failures return `stale`, no links; no cache
write on reads. Public fields are rebuilt from a whitelist, never raw cache.
States reserved by contract: absent, identifying, no_address, addressed, ambiguous,
stale, error. No state means phone connected or capture ready.

## Existing HTTP API and UI

`GET /api/phone/links` retains `mode`, `current`, `links`, `interface`, `address`,
`origin` for old clients, and adds `configured_transport`, `current_transport`,
`state`, `reason`, `device_id`, `boot_id`, `controller_identity`, link `iface`.
Legacy `mode=wifi` is retained only in configured ADB mode; it is not an ADB
health assertion. Current transport uses the HTTP socket's local address, **not
Host**. Loopback is reported as loopback, never proof of ADB. Tether mode publishes
only qualified fresh tether addresses, never Family/Wi-Fi fallback links. Failure
or ambiguous/stale state publishes no tether URL. HTTP polling errors clear UI
addresses. Controller identity reuses existing privacy instance and boot; device ID
reads existing `/opt/egorecord/DEVICE_ID`, validated or null (unknown), not phone
serial. This is target presentation, not cryptographic identity attestation.

Open the displayed Pi USB-IP URL **manually on the intended phone**. No control
token is encoded in URLs here. The existing reference-gallery QR/key is not a
control credential. Switching origin requires fresh authentication/sessionStorage
and existing display registration; no automatic START retry is added.

**Security:** repository `devices.yaml` currently defaults
`access_code_required: false`. Reachable HTTP can therefore permit anonymous
control according to existing configuration. This feature does not silently change
global authentication. Ordinary HTTP is unencrypted; private/wired IP is not a
security guarantee. A deployment review must explicitly accept or address this.

### Screen limitations

Wake Lock is a Web API, not ADB/system screen control. Button reports actual
`unsupported`, `insecure`, `requesting`, `held`, `released`, `denied` state; only a
fulfilled held sentinel is `held`, and OS release is reflected. Denied requests
are not repeatedly retried on status updates; toggle off/on for an explicit retry.
HTTP private USB IP normally is **not a secure context**, unlike localhost.
Keep the page foreground and manually review screen timeout/power settings. These
are warnings, **not guarantees of screen power, kiosk behavior or STOP delivery**.
No toast/bright overlay is added during privacy; existing privacy cover and reveal
conditions are unchanged. A black webpage is not Android physical screen-off,
notification suppression, or an assurance the browser stays schedulable.

## Offline NetworkManager candidate only

```sh
python3 scripts/phone_tether_profile.py --config /path/reviewed.json \
  --id-path platform-REVIEWED.usb-usb-0:2.3:1.0 --driver rndis_host \
  --nm-version 1.42.0 --candidate-subnet 192.168.77.0/24 \
  --routes-json /path/offline-route-inventory.json
```

Default is stdout/dry-run. Optional `--staging /explicit/new/candidate.nmconnection`
writes one new file outside `/etc`, no overwrite/symlink. No nmcli, network version
probe or activation. Exact reviewed USB udev `ID_PATH` plus driver required;
wildcards/driver-wide matching refused. Version is explicitly operator-attested,
not discovered: conservative support floor NM 1.42; unknown/older refused. See
[NM match documentation](https://networkmanager.dev/docs/api/latest/settings-match.html).
Selector VID/PID/optional serial additionally qualify observer discovery: **NM
match.path/driver alone do not enforce VID/PID**. Operator must verify the same
qualified device before any separately approved manual activation.

Candidate uses autoconnect false, IPv4 auto, never-default, ignore-auto-dns,
ignore-auto-routes, IPv6 disabled and a feature-specific deterministic UUID. No
other connection UUID is removed. Existing generic Ethernet autoconnect profiles
may compete: inspect and resolve manually during the approved window; this tool
does not modify them. Route inventory is explicit JSON list (`dst`, e.g.
`[{"dst":"192.168.20.0/24"},{"dst":"default"}]`). Any nondefault subnet overlap
blocks generation. Inventory must be complete and current, candidate DHCP subnet
must be confirmed; these flags alone cannot prevent overlapping connected routes.
Actual DHCP/connected-route behavior, link reachability and existing Family/Guest
routes/DNS preservation require device validation. Generated file is **not an
authorization to activate**, nor an enforcement daemon for future DHCP changes.

## Installation review, switching, rollback

The new installer is **plan-only**: no `apply`, root platform, systemctl, ADB,
service activation, global installer reuse or automatic conflict-stop. The
startup gate only reads the opposite unit's `systemctl show ActiveState` (never
start/stop): active/transitioning/unknown state refuses startup; inactive/failed
permits the configured mode. The plan stages
only `/opt/headring-phone-transport/*`, config, new observer unit and a dedicated
ADB `ExecCondition` drop-in. It does **not** stage/deploy the WIP collector. The
HTTP/common-module changes need a separately reviewed collector release.

```sh
python3 scripts/install_phone_transport.py plan --staging /path/new-review-dir \
  --config /path/reviewed.json --snapshot-root /path/offline-target-fixture
python3 scripts/install_phone_transport.py rollback-plan --staging /path/new-review-dir
```

`--snapshot-root` optional: only an explicit offline filesystem fixture is read,
never implicitly inspect real system destinations. Without it, existing-file
status is `not_inspected`, not a backup claim. Manifest records proposed mode,
owner/group, SHA256, target existence/status and original mode/hash, with private
backups when available. Identical planning is idempotent; changed payload, backup,
or target fixture causes conflict refusal. Protect the review directory: backups
can contain private configuration. **Nothing is installed, so rollback-plan has
zero actions and cannot claim to have restored a device.** Future applied install
needs its own applied manifest and post-install conflict validation. No broad
delete or arbitrary connection removal is provided.

After separate deployment approval, operator procedure (not executed by tools):

1. Verify exact target/release, backups, authentication boundary, authoritative
   safe idle/stop proof and no active recording/arming. Do not switch while busy.
2. Explicitly stop old transport in the controlled window **before** replacing
   config. Refuse new-mode activation if the other mode is still active. No
   `Conflicts=` or automated service killing; startup gates do not stop an already
   running unit. `headring-usb-data-host` remains unchanged.
3. Review/apply only approved files/account permissions and narrow NM candidate;
   independently verify topology, actual subnet overlap, routing/DNS, competing
   profiles. Manually enable phone tether; start selected observer only once other
   mode is inactive. Manually open USB URL and reauthenticate/register the display.
4. Verify reference image, previews and full START→ACK→capture→long-press STOP→
   saved/QC/reveal lifecycle under load before declaring accepted.
5. **Return to ADB:** verify authoritative safe idle, explicitly stop observer and
   deactivate only this feature's NM UUID if it was manually activated. Restore
   reviewed `mode=adb` configuration (or approved original missing config), verify
   other-mode inactive, then explicitly start existing ADB service. User restores
   phone USB mode/debug authorization manually as needed. Restore owned files only
   against applied post-install hashes; later modifications require manual review.
   No reverse mapping/protocol/auth/privacy changes are required.

## Executed offline evidence / remaining acceptance

Tests in `orangepi/tests/test_phone_tether*.py` and `test_phone_tether_ui.cjs`
cover sysfs RNDIS/NCM selectors/multi/wrong device/Family exclusion, unplug/rename,
no IP, cache TTL/boot/schema/paths/permissions/secret redaction, profile guards,
plan backups/idempotence/conflicts, socket-vs-Host and auth, UI transport and actual
Wake Lock failure/release/privacy. Python HTTP tests use local loopback fixtures,
not devices. Node tests execute extracted real app logic, not physical browsers.

Offline run on 2026-11-12:

| Scope | Base `01bd501` | Feature |
|---|---|---|
| Selected existing Python phone/USB/STOP regressions | 92 run, 90 pass, 2 fail | same failures; zero new |
| New Python fixtures (stdlib; also run with project Python 3.11) | n/a | 33 pass |
| Existing Node regression scripts (runner file-level tests) | 9 pass | 9 pass |
| New Node UI cases | n/a | 8 pass |

Combined final project-Python run: 125 tests, 123 pass, the same two existing
failures. Combined Node runner: 17 tests, all pass. No full-repository pass is
claimed. Reproduce new fixtures with
`python3 -m unittest discover -s orangepi/tests -p 'test_phone_tether*.py' -q`
and `node --test orangepi/tests/test_phone_tether_ui.cjs`.

Astra independently reviewed `7e49950` (33 Python / 8 Node pass), reporting no
blocking/high findings and one medium Wake Lock sentinel-lifecycle issue. The
follow-up fix discards already-released request results, detaches on explicit
disable before awaiting release, preserves new sentinels against late old events,
and invalidates pending requests on disable (including disable/re-enable races).
Four extracted-real-code regressions failed before the fix and pass afterward:
current focused Python 33/33; Node UI 12/12 (original 8 plus 4); related Node
runner 21/21 (12 cases plus 9 existing script wrappers). Privacy/busy gates,
START and device/deployment scope are unchanged. No full-product rerun or device
acceptance is claimed for this follow-up.

**Not tested:** any phone model/OEM/Android/browser, kernel drivers, no-SIM/no-data
tether enabling, DHCP, USB power/roles, actual ID_PATH, network activation/default
route/DNS preservation/overlap behavior, generic profile competition, manual URL
delivery, real screen lock/background behavior, long-press physical STOP, privacy
under real media load, performance, device deployment or production rollback.
These remain mandatory acceptance gates; successful offline tests do not close
them. Baseline comparison and exact commands/counts are in the implementation
handoff report; two pre-existing `test_usb_auto` assertions fail identically at
base and feature (transport_id shape and saved-binding preference).
