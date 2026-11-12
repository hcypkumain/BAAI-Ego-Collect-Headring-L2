# Governance contract C0 (local evidence v1)

Owner: main/M0. Workers consume this exact commit; changes require main's explicit
C1 and a coordinated baseline update. C0 is **not** a CLI, deployment, pipeline
migration, new device wire protocol, or live verification. No phase4/4A import.
All examples are synthetic; never use the fixture inventory as production config.

## Root imports and exact API

Python 3.10+ syntax, standard library only for the pure modules. Run from repository
root (`PYTHONPATH=.`); no module changes `sys.path`. Package initializers do not
import adapters. `host.standardizer.worker` is an **archive, not import-safe** at
C0; A must first remove its initialization effects. No config, trust, credential,
environment, filesystem, DNS, DB, process, thread or network access occurs in the
shared modules or factories. Python's ordinary module source loading is distinct
from application/runtime I/O.

```python
# host.common.device_registry
@dataclass(frozen=True)
class UploadGrant:
    operation: str
    destination: str
    reason: str
    owner: str
    expiry: str | None

@dataclass(frozen=True)
class Device:
    device_id: str
    ssh_alias: str
    address: str
    ssh_user: str
    host_key_alias: str
    capabilities: tuple[str, ...]
    enrolled: bool
    receive_member: bool
    monitor_enabled: bool
    upload_grants: tuple[UploadGrant, ...]

@dataclass(frozen=True)
class Registry:
    schema_version: int
    digest: str
    devices: tuple[Device, ...]

def parse_registry(value: Mapping[str, object]) -> Registry: ...
def select_devices(registry: Registry, selected: Sequence[str], *, purpose: str) -> tuple[Device, ...]: ...
def receive_ids(registry: Registry) -> frozenset[str]: ...
def upload_allowed(registry: Registry, device_id: str, *, destination: str, now_utc: datetime) -> bool: ...
```

Dataclasses validate direct construction as well as parsing. Collections must be
tuples in objects and arrays in serialized inputs. Validation raises `ValueError`;
unknown/denied upload scopes or IDs return `False` (invalid clock raises).
No credentials belong in `Device`. SSH/trust paths belong only in `ProbePolicy`.

### Registry, identities, permissions

Registry input is a Mapping **only**, with exactly `schema_version` (strict int 1,
not bool/float) and `devices` (array, at most 64; empty registry allowed). Each
device/grant requires exactly its declared fields; null is accepted only where
annotated. All three enrollment flags are strict bools, not 0/1/strings. IDs match
`^cp_[0-9]{4}$` exactly; no normalization/alias fallback. SSH identifiers and
capabilities match `[A-Za-z0-9][A-Za-z0-9_.-]*`; capabilities are unique descriptive
labels, **never authorization or command generators**. Addresses are explicit
IPv4/IPv6 (IPv6 zones rejected); no hostnames, DNS, scanning or address discovery.
Reject duplicate IDs, SSH aliases, host-key aliases, canonical addresses (including
IPv4-mapped IPv6), and cross-device alias/ID/address identity ambiguity. A device's
own aliases may coincide; identities may not overlap another device's identities.

The digest is SHA256 of UTF-8 canonical JSON of the validated **input configuration**
`{"schema_version":1,"devices":[...]}`, excluding digest. Canonical means Python
`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(',', ':'),
allow_nan=False)`. Array order is preserved (not sorted). No NaN, Infinity,
overflow-to-infinity, duplicate JSON keys, nonstring object keys, or Unicode
surrogates. Registry.digest validates against the devices; it is not installed or
runtime proof. Root CLI must explicitly read its requested file, run
`host.common.strict_json.strict_json_loads(text)` **before** `parse_registry`;
ordinary `json.loads` cannot detect duplicate keys once collapsed. The pure helper
does not open files. `canonical_json(value) -> str` and
`strict_json_loads(value: str) -> object` are shared strict serialization helpers.

`select_devices` requires nonempty, duplicate-free explicit selection, preserves
selection order, rejects unknown IDs, and accepts only:

|purpose|required gates|
|---|---|
|monitor|enrolled AND monitor_enabled|
|receive|enrolled AND receive_member|
|standardize|enrolled AND receive_member|

`receive_ids` returns exactly the enrolled/receive intersection as a frozenset.
Disabled enrollment is always denied, even with flags/grants present.

### Upload destination scope: frozen decision for A

`destination` is the **exact project root scope**, `EGOL2` or `EGOL3_hand`,
consistent with the two roots in existing `cloud_scope`. It is **not** a full
dataset path and not a wildcard/session glob. A must first apply existing
`dataset_path(target)` validation to the full target, require at least
`root/device_id/...`, validate that `parts[1]` equals the actual source/queue
device_id, and then call shared `upload_allowed(registry, device_id,
destination=parts[0], now_utc=...)`. A must check source/device binding, not trust
an arbitrary queue string. Existing profile, manifest, project binding, cloud
verification, deletion, and retention guards remain mandatory. Scope grants
authorize upload only: never listing/deletion or another project/device.

Examples (synthetic migrated cp_0007 grant for EGOL2):

|input|outcome|
|---|---|
|shared call device=cp_0007, destination=EGOL2|allowed if enrolled + active grant|
|shared call device=cp_0042, destination=EGOL2|denied despite monitor/receive flags|
|shared call destination=EGOL3_hand without that exact grant|denied|
|shared call destination=EGOL2/cp_0007/session|denied; full paths are not shared scope inputs|
|shared call destination=EGOL2/* or EGOL2evil|denied|
|A full target EGOL2/cp_0007/2026-11-13/L3-2026-11-13-40177, source cp_0007|validate full path/binding, then evaluate EGOL2 grant|
|A same target, source cp_0042|reject binding before grant check|
|A target EGOL2/../cp_0007/session or EGOL2/cp_0007|reject unsafe/incomplete dataset|

Only operation `upload` is supported. Grant reason and owner are nonempty,
control-free text. `expiry` is UTC RFC3339 (`Z` or `+00:00`, optional 1–6 fractional
digits); naive/non-UTC/invalid calendar times are rejected. A grant expires at
`expiry <= now_utc`. The supplied clock must be timezone-aware with zero UTC
offset; the shared gate never reads the clock. `expiry=None` requires an explicit
`legacy:` prefix with nonblank explanation. Duplicate operation/destination grants
are rejected. No fallback, automatic scope expansion or receive-derived grant.
Legacy four-device (7/11/13/14) authorization is an **explicit migration input** to
A, not parser defaults. Production config migration has **not** happened in C0.

`contracts/v1/device-registry.schema.json` supplies strict structural validation;
semantic parser validation is mandatory for duplicate identities/scope grants,
integer-vs-float distinctions and exact IP/time semantics. JSON Schema format
checks must be enabled explicitly; some installed validators lack optional
date-time dependencies. Tests supply a pure UTC date-time format check when
jsonschema is available. No new dependency installation is needed. The example
`configs/device-registry.example.json` has all authority disabled and no grants.

## Evidence, results and policy

```python
# host.common.governance_contract
@dataclass(frozen=True)
class Evidence:
    value_json: str | None
    source: str
    observed_at: str | None
    quality: str
    reason: str | None

@dataclass(frozen=True)
class ProbeResult:
    registry_digest: str
    device_id: str
    generation: int
    identity_verified: bool
    status: str
    fields: tuple[tuple[str, Evidence], ...]

@dataclass(frozen=True)
class ProbePolicy:
    ssh_config: str
    known_hosts: str
    identity_path: str
    component_paths: tuple[tuple[str, str], ...]
    disk_mount: str | None
    timeout_ms: int = 5000
    maximum_bytes: int = 1048576

def validate_evidence(value: Evidence | Mapping[str, object]) -> Evidence: ...
def validate_probe_result(value: ProbeResult | Mapping[str, object]) -> ProbeResult: ...
def validate_probe_policy(value: ProbePolicy | Mapping[str, object]) -> ProbePolicy: ...
```

These validators are pure and reject unknown serialized fields. Policy mappings
may omit only the two declared numeric defaults. Serialized tuples use JSON
arrays. For public JSON use `canonical_json(dataclasses.asdict(obj))`; deserialize
with `strict_json_loads`, then the appropriate validator. No pickle. No new shared
evidence JSON schema or adapter methods are introduced by C0.

`quality` is exactly observed/unknown/stale/error. Unknown/error has
`value_json=None`, never the string `"null"`, an invented zero, empty state, or
another device's evidence. Observed/stale requires a non-null canonical JSON value
and UTC sample timestamp; timestamps describe sampling, not inferred freshness.
`source` is a nonempty control-free logical evidence label, never secret contents.
`reason` and timestamps are nullable; source/reason must not echo raw credentials.
CLI computes age with its own monotonic clock; no device-side controller sampling
age means freshness unknown, never guessed from PID or an unproven status file.

Results always retain exactly six unique keys (pair order is not authority):
`boot_id`, `ram_available_bytes`, `disk_available_bytes`, `controller_state`,
`installed_components`, `runtime_components`. Boot/controller values are nonempty
JSON strings or absent evidence; RAM/disk are strict nonnegative JSON integers,
not bool/float. Component values are JSON objects (known-ID subset allowed):

```json
{"collector":{"sha256":null,"source":"fixture:not-loaded","status":"unknown"}}
```

Each component entry has exactly sha256/status/source. Status is observed/unknown;
observed requires lowercase 64-hex SHA256, unknown requires null. The nine allowed
component IDs shared with B are **collector, phone_link, phone_media, helper,
systemd_udev, host_qc, monitor, standardizer, uploader** (`COMPONENT_IDS`). No
source-to-installed or installed-to-runtime substitution. A file hash is only file
evidence (source label `file:...`); absent complete component algorithm proof, B
compares it only with the same BOM file evidence, never claims entire-component
verification. Runtime stays unknown without evidence of actually loaded bytes.

`registry_digest` is lowercase SHA256; generation is a strict nonnegative int;
identity flag is strict bool. Result status is exactly ok/partial/unavailable/
identity_rejected/timeout/quarantined. Unverified identity may return only
unknown/error null-valued fields. `ok` requires verified identity;
identity_rejected cannot claim verified identity. Partial may have all unknown
fields; status never invents controller truth. CLI **and** worker must match the
expected digest/device/generation before accepting a result; syntax validation
cannot establish that match. Verified identity requires BOTH strict known-host
SSH binding and main-approved pure identity-file match, not self-report alone.

Policy trust/identity/source paths are opaque canonical absolute POSIX strings;
validation does not open, resolve symlinks, inspect trust, or run shell. Control
characters, backslashes, dot/traversal or repeated-separator noncanonical paths
are rejected. `component_paths` may be an empty/subset tuple of the nine IDs,
with unique IDs **and** unique paths; no auto discovery or response-added paths.
No policy value is a shell command. C must pass validated paths as data to a fixed
bounded reader, never interpolate executable shell. `disk_mount=None` is the
default caller choice (field remains explicit); no implicit disk/volume scanning.
Timeout is strict int 1..5000 ms; maximum_bytes strict int 1..1048576. No credential
material is included. Production trust and readonly SSH argv remain C/main gates,
not promises made by this pure path validator.

### Serialized result example

```json
{
  "registry_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "device_id": "cp_0042",
  "generation": 1,
  "identity_verified": false,
  "status": "identity_rejected",
  "fields": [
    ["boot_id", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}],
    ["ram_available_bytes", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}],
    ["disk_available_bytes", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}],
    ["controller_state", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}],
    ["installed_components", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}],
    ["runtime_components", {"value_json": null, "source": "fixture:identity", "observed_at": null, "quality": "unknown", "reason": "identity mismatch"}]
  ]
}
```

An observed RAM Evidence serializes as
`{"value_json":"1048576","source":"proc:meminfo","observed_at":"2026-11-13T00:00:00Z","quality":"observed","reason":null}`.
The digest above is synthetic, not a current configuration digest.

## Shared fixtures (main-owned, do not copy/fork)

`host.qc.tests.governance_fixtures` exposes:

```python
sample_grants() -> dict
sample_device(device_id='cp_0042', *, index=1, **overrides) -> Device
sample_registry(device_ids=DEFAULT_DEVICE_IDS, *, receive=None, monitor=None,
                enrolled=None, grants=None) -> Registry
sample_evidence(value=None, *, source='fixture:unknown', observed_at=None,
                quality=None, reason=None) -> Evidence
sample_probe_result(device_id='cp_0042', *, registry_digest=None, generation=1,
                    identity_verified=True, status='partial', fields=None) -> ProbeResult
sample_probe_policy(**overrides) -> ProbePolicy
```

Default six-device fixture = five synthetic devices 7/11/13/14/15 plus 42. All can
monitor/receive; only 7/11/13/14 have explicit synthetic legacy upload grants for
the two exact roots. `grants={}` denies all uploads; `None` chooses fixture grants.
`receive`, `monitor`, `enrolled` accept explicit ID sequences; empty means none,
not defaults. Factory addresses use RFC5737 `192.0.2.0/24`; trust references live
under nonexistent `/synthetic/...`. No real inventory/history/trust/config is
copied. `sample_device` takes immutable tuples for overrides; sample_registry's
grant mapping accepts serialized grants or UploadGrant instances. Result fields
is an optional name->Evidence override mapping; all remaining fields stay unknown.

## Source provenance and required A caller adaptation

The sole public source selection record is
`deployment/manifests/host-source-provenance.json`, containing logical source
labels, reviewed source SHA256, artifact path/SHA256, selection and reasons. It is
not an installed/runtime manifest. Five sources were rehashed before copy and
matched the fixed plan. Local private mapping is kept outside Git only.

- `Q/receiver_state.py`: retain certified base; fixed-list additions are deferred
  to A's registry injection. Reviewed same-device `busy_devices` receive claim
  protection is deferred to A (do not lose it when adapting claims).
- `Q/handoff_receiver.py`: Q is older/weaker than certified. Reject the 319-line
  diff that removes source-QC binding, signature/required-device v2 qualification
  and artifact/deletion protections. **Certified bytes retained, not synced**.
- `Q/host_storage.py`: source hash equals certified; no copy needed.
- `Q/adapter/cloud_upload_cli.py`: retain certified, do not import partial live
  performance/file_upload_pool/private-config dependencies. A must explicitly
  reintroduce the independent four-device authorization using grants, not enroll
  every receiver. No live copy, no performance migration in C0.
- `standardizer/worker.py`: derived archive, exactly one private QC path replaced
  by `/UNCONFIGURED/headring-qc`; original and derived hashes are distinct. It
  still reads config/data on import and captures default context. **Never import
  or run this archive in C0**. No invented DATA default. A purifies it before use.
  Converter, SDK and venv are external/unbundled/unverified dependencies.

A must adapt Store construction, HandoffReceiver construction/locks/legacy
filtering, standardizer context and verify/make callers, and upload entry/seed/
claim/upload gates. A must report legacy callers outside its allowlist; main
adapts `services/headring-qc/service.py` only after A merges. All Store test callers
must pass the same fixture registry. A's uploader helper consumes full dataset
paths and forwards only validated project roots to this shared gate. B/C consume
these dataclasses directly, with no alternative registry/evidence schema. No
pipeline callers were altered in C0; old production behavior is not claimed fixed.

## G0 local verification

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s host/common/tests -p 'test_*.py'
git diff --check
```

Tests cover strict parser/JSON/schema, identities/IPv4/IPv6, selection/capabilities,
grants/expiry, immutable/nullable types, evidence/policy/identity validation, and
pure package/module reload plus API calls under file/network/thread/process spies.
AST compile archived code without importing it. C0 does not assert host runnable,
live read verified, production pipeline migration, START or impedance repair.
