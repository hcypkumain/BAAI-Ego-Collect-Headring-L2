# Release validation (B / C0 consumer)

Offline code only. No device, production HTTP/DB, private configuration, service,
ADB or credential access was used. This is not production migration or live read
verification. Main owns the public source/release manifests and executable build
integration; these modules do not open configuration on import.

## Frozen entry points

Import from repository root (`PYTHONPATH=.`):

```python
from tools.fleet_maintenance.component_bom import build_bom, compare_components
from tools.fleet_maintenance.release_registry import validate_release, resource_digest
```

Signatures are those in CONTRACT: keyword-only `build_bom(source_manifest,
read_blob, overlays)`, keyword-only `validate_release(release, router, resources)`,
and `compare_components(bom, observation)`. Only **empty tuple** from validation
permits selection. No file discovery, worktree HEAD resolution or registry loading
occurs in these APIs. Malformed/missing data blocks. Approvals are reviewed input,
not signatures or authentication: main must load only its reviewed manifest;
an attacker allowed to replace both approvals and router is outside this checker.

## Exact source_manifest input v1 (B-local, not a new shared schema)

Root keys are exactly `schema_version` (strict integer 1), `base_commit` (40 lowercase
hex), `components` (nine records, each exactly `component_id`, `files`, `approval`).
Every file requires exactly `ref`, `path`, `sha256`, `provenance`, `resource`.
`ref` is a full immutable Git commit; `path` and `resource` are canonical relative
paths. `sha256` is checked against **actual bytes** from `read_blob(ref, path)`.
`resource` names the corresponding bytes in the later validator resources map.
Provenance is a public logical label, never a private absolute path. For C0
archives use `artifact.source`, e.g. `Q/receiver_state.py` or
`standardizer/worker.py`, from the sole main-owned
`deployment/manifests/host-source-provenance.json`. Keep reviewed upstream
`source_sha256` distinct from selected/modified artifact SHA256. Do not copy Q
configuration or infer installation from archival provenance.

The following is a precise executable **synthetic example**, not approval for a
real release. Main supplies the two explicit immutable blob readers/records; the
example uses the actual C0 archive and preserves its source label:

```python
import hashlib, json
from host.common.governance_contract import COMPONENT_IDS
C0 = '1404ba567009058e40d4294f8555a66e5ee8ca86'
# read_blob is main's bounded, explicit Git-object reader: (commit, relative_path)->bytes.
provenance = json.loads(read_blob(C0, 'deployment/manifests/host-source-provenance.json'))
artifact = next(a for a in provenance['artifacts'] if a['source'] == 'Q/receiver_state.py')
data = read_blob(C0, artifact['artifact'])
assert hashlib.sha256(data).hexdigest() == artifact['artifact_sha256']
def pending(component):
    return {'status':'unknown', 'scope':[component], 'reason':'not reviewed', 'expiry':None}
source_manifest = {
    'schema_version': 1, 'base_commit': C0,
    'components': [
        {'component_id': c, 'files': [], 'approval': pending(c)} for c in COMPONENT_IDS
    ]
}
host = next(c for c in source_manifest['components'] if c['component_id'] == 'host_qc')
host['files'] = [{
    'ref': C0, 'path': artifact['artifact'], 'sha256': artifact['artifact_sha256'],
    'provenance': artifact['source'], 'resource': artifact['artifact']
}]
bom = build_bom(source_manifest=source_manifest, read_blob=read_blob, overlays=[])
resources = {artifact['artifact']: data}
```

All nine component records must remain: collector, phone_link, phone_media, helper,
systemd_udev, host_qc, monitor, standardizer, uploader. Empty files explicitly means
external/missing/unverified source: source hash/ref are null and status unknown.
Listing nine records is coverage, **not** an installed/full-service claim. In the
example a single receiver file is only a selected host_qc **file set**, not proof
of a complete runnable host pipeline or converter/SDK closure.

Output source has `ref`, `sha256`, `provenance`, `status`, `files`. Its SHA256 is a
file-set digest: SHA256 of shared canonical JSON of the path-sorted array of
`{"path":...,"sha256":...}` records. It is never an arbitrary single-file hash
presented as a whole-component hash. Individual files retain their own exact ref,
hash, provenance, resource. Top-level source ref is the base selection, not a
claim that overlay files came from that base. Installed/runtime start separately
as `{sha256:null,status:"unknown",evidence:null}` even with verified source.

## Fixed collector overlay, not a generic payload API

For collector: base is exactly
`f5d03b7b5fb197eb76e78610a0bce672e5cc4853`; enumerate all 236 files from the
SHA-pinned stable manifest and read each at that commit. Refuse unknown baseline.
An overlay descriptor has exactly:

```python
overlay = {
    'overlay_id': 'eeg-impedance-advisory-36d8059918ce',
    'ref': C0,  # immutable commit containing the reviewed original resource bytes
    'approval': {'status':'approved', 'scope':['collector'],
                 'reason':'SYNTHETIC TEST ONLY', 'expiry':'2099-01-01T00:00:00Z'},
    'resources': {
        'stable_manifest.json':'tools/fleet_maintenance/resources/manifest.json',
        **{name: 'tools/fleet_maintenance/resources/eeg-impedance-advisory-36d8059918ce/'+name
           for name in ('manifest.json','bundle.zip','transaction.py','bootstrap.py')}
    }
}
```

Builder checks original manifest/bundle/transaction/bootstrap exact SHA256 pins,
stable manifest pin and 236 count, exact seven archive members, five unique delta
paths, before hashes, after hashes and byte sizes. It actually extracts/rebuilds
the five after entries; it does not read R HEAD's before bytes and relabel them.
The five changed files have the resource commit ref and registered overlay label.
Their validator resource keys are `payload/<repository-path>`; add the extracted
after bytes to `resources` with these keys (the pure `overlay_payload` helper
returns checked bytes). Unchanged keys are their repository paths. Also include
the five resource bytes under the descriptor's **short keys** shown above.
Only [] or this single overlay is supported. 61f/7fcb candidates remain
unregistered/unapproved; no candidate was imported, copied or registered here.
An independent host-only release with no candidate reference is not blocked by
the existence of an orphan candidate elsewhere.

## Release and router exact example

After main reviews scope, replace pending approvals for components **in scope**.
Release approval must have identical scope (including order), reason and expiry.
Component approval scope must be exactly its singleton component ID. Overlay
approval scope must be exactly `["collector"]`. Expired/non-UTC timestamps block.
Null expiry is allowed only with an explicit nonempty `legacy:` reason.

```python
scope = ['host_qc']
approval = {'status':'approved','scope':scope,'reason':'SYNTHETIC TEST ONLY',
            'expiry':'2099-01-01T00:00:00Z'}
next(c for c in bom['components'] if c['component_id']=='host_qc')['approval'] = dict(approval)
release = {
    'release_id':'synthetic-host-review', 'base_commit':C0, 'overlay_ids':[],
    'component_BOM':bom, 'scope':scope, 'reason':approval['reason'],
    'expiry':approval['expiry'], 'approval':approval,
    'resource_digest':resource_digest(resources)
}
router = {release['release_id']:release['resource_digest']}
blocked_codes = validate_release(release=release, router=router, resources=resources)
assert blocked_codes == ()
```

resource_digest is SHA256 of shared canonical JSON mapping every resource key to
its byte SHA256. Thus router, release and supplied bytes must agree. File-set
hashes and every in-scope resource hash are independently checked; collector also
requires the original base/overlay manifest and complete reconstructed 236 paths.
Missing resources, unknown source, component omission, wrong scope, expiry,
unregistered overlay or tampering block; rehashing only the outer route cannot
hide a wrong original collector pin or a mismatched source file.

The complete nested BOM is validated before eligibility, including components
outside release scope. Exact required keys are enforced at BOM root, overlay,
overlay resources, component, source, file, installed/runtime, their evidence,
and approval levels; missing/extra keys block with `bom_schema`. The version is
a strict integer (bool is not int), refs are full lowercase 40-hex commits and
hashes lowercase 64-hex. Provenance must be a nonempty, control-free logical label,
not an absolute/private path, backslash or dot/traversal segment. Source provenance
must equal the sorted unique labels of its files; source file-set digest and base
ref must agree. Unknown source has empty files/provenance and null ref/hash;
unknown installed/runtime has null hash/evidence. None of these fields may be
omitted, even outside scope. Approval statuses are approved/unknown/rejected with
explicit scope array, nonempty control-free reason and null or valid UTC expiry.
Observed installed/runtime evidence requires exactly source, scope=`file`, strict
boolean matches_source and valid UTC observed_at, matching a declared same file.
These rules also admit valid `compare_components` output without inventing runtime.

Representative codes: `component_coverage`, `base_unknown`,
`collector_baseline_unknown`, `overlay_unregistered`, `overlay_approval`,
`collector_reconstruction`, `source_unknown`, `source_resource_mismatch`,
`resource_digest_mismatch`, `router_resource_mismatch`, `release_approval`,
`component_approval_scope`, `malformed_or_missing_resource`. Multiple codes may
be returned, stable check order; callers should display them, not whitelist away
unknown codes. Approval time is evaluated at invocation UTC, not cached forever.

## Probe comparison: file evidence only

`compare_components` returns a copy, clearing any previous installed/runtime
observations. Main must first match expected registry digest/device/generation
(the two-argument frozen API has no expected identity parameters). C0 validation
and identity_verified are also required. Missing, stale, error or unknown evidence
stays unknown. Installed proof must use exact `file:<BOM-relative-path>` label.
Different file labels cannot be compared merely because hashes coincide.
Returned observed hash is explicitly file-scoped with source label, sample time,
and matches_source boolean, never component-wide verification.

Runtime requires independent observed **loaded bytes** evidence labelled
`loaded:file:<same-BOM-relative-path>`. A normal on-disk `file:` hash in runtime
is rejected as loaded-byte proof. C's current file-only probe cannot manufacture
this evidence and should keep runtime unknown. Main must bind probe absolute
policy paths to these reviewed logical labels without device-supplied remapping;
until that reviewed binding exists, mismatch conservatively stays unknown.
Source, installed and runtime never substitute for one another.

## Registry CLI migration and main-owned caller changes

From root, use explicit registry and canonical targets, even offline:

```sh
PYTHONPATH=.:tools/fleet_maintenance python3 tools/fleet_maintenance/fleet.py \
  --registry <explicit-local-registry.json> --targets cp_0042 --offline-plan \
  --state-dir <isolated-output-directory>
```

No default cp_0014/cp_0018. Missing targets/registry exits in argparse before file
read, Git build or SSH. Root main uses strict_json_loads then parse_registry and
select_devices(purpose='monitor'); unknown/unenrolled/monitor-disabled selection
fails. Legacy aliases `cp0042` are rejected. Legacy registry-free calls fail
closed, not fallback. `--address` can only repeat the registry address, never
override it. SSH config's user and host-key alias must match the selected Device;
transport uses Device.ssh_alias/address. The final wrapper argv has exactly one
explicit HostName/HostKeyAlias/User fixed to that Device, plus
StrictHostKeyChecking=yes and BatchMode=yes. It strips earlier instances of these
options and puts the fixed values first, honoring OpenSSH's first-value semantics;
neither online.SSH's second config read nor actual OpenSSH's later config parse
can change effective identity. Unexpected transport argv shapes fail closed.
online.py is unchanged; secure sudo PTY handling is retained. SSH password prompts
are disabled by the wrapper's BatchMode (no fallback). Deprecated
`--approve-unknown-baseline` always errors; old unknown-approved plans cannot
apply/verify. Stable shadow, downgrade, preflight/transaction and pins remain.

Both stable and overlay newly created accepted plans carry two additional fields:
`registry_digest` (exact Registry.digest) and `registry_device` (exact object with
device_id, ssh_alias, address, ssh_user, host_key_alias from the selected Device).
`plan.device` must also equal that device_id. Missing/wrong digest or identity,
including an old plan after any registry change, fails with a fresh-plan-required
error before stage/apply/verify. An address match alone is insufficient. Old plans
must be regenerated, not patched or grandfathered.

Checks are not confined to argparse: fleet's `stage_registry_plan`,
`verify_registry_components` and `verify_registry_plan` each call
`require_registry_plan` at their own boundary. Delta's internal staging helper now
requires `stage_apply(..., command_file=None, *, registry: Registry, plan)` and
checks the binding **before** TTY/stage/remote operations. Main adapters and local
test spies calling that helper directly must pass a freshly bound reviewed plan;
there is no registry-free compatibility path. The three frozen public BOM APIs
and C0 contracts remain unchanged; this does not require C1.

Delta main now requires `main(args, parser, *, registry: Registry)`. Its cache is
`<state-dir>/observations.json`, **not inventory.json**. It may contain only
last_verified_at/last_checked_at/policy/status/applied_release; identity/address
fields or IDs outside registry are rejected. Old inventory is not auto-migrated.
Cached status is historical, not live/offline truth; errors report
unavailable_or_rejected with sample time, not invented controller state.

`web_config.setup(*, registry, selected, config)` is pure, with no file reads,
directory creation, automatic defaults or second inventory. Config accepts only
ssh_config (required), bind_interface and bind_address. It returns selected
devices plus registry_digest and identities. **Main must adapt**:

- `tools/fleet_maintenance/web.py: main -> web_config.setup()`;
- `tools/fleet_maintenance/broker.py: main` and
  `Broker.handle` (the setup recheck near line 55): inject the same explicitly
  loaded registry/selection/transport config, fail on digest/identity changes;
- `jobs.Jobs`, operations/broker SSH construction and UI command builders: retain
  registry identity binding and pass explicit --registry/--targets to fleet;
  never merge observed cache into configured identities. These callers are not
  patched by B and must not run old registry-free UI/broker as migrated code;
- build_governance_release/governance_cli and public manifests: supply reviewed
  source records, packaged resources/router, approved scope, expected observation
  tuple and logical evidence-label binding. B never edits these shared files;
- root packaging/import setup: legacy fleet/delta/online use bare sibling imports;
  root-importable new BOM/validator do not. Main owns application path setup.

A owns the pure legacy migration tool; B does not duplicate its truth or migrate
production config. Any service/default inventory migration remains main's work.

## Tests and baseline delta

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover \
  -s tools/fleet_maintenance/tests -p 'test_governance_release.py'
```

Original suite C0: 107/107 passed before these changes. Initial B had 38/38 new
tests; Astra G1-B follow-up adds 12 independent boundary test methods, so the
current B suite is **50/50** and shared C0 suite **16/16**. The 12 regression tests
were also executed against original immutable B implementation `4f2c6e4` in memory
(without resetting files): 45 failing checks including subtests, one missing-new-
helper-signature error; corrected implementation passes all 12. They cover the
five exact Astra malformed-BOM counterexamples, nested bool/extra/invalid labels,
overlay shape, valid unknown/compare controls, 24 stable/overlay real-main plan
create/apply/verify combinations with only local spies, direct helper bypass and
two-read SSH identity drift. No actual stage/apply/SSH process ran.

Current complete fleet suite: **157 executed, 148 passed, 9 unmigrated old-test
errors**, zero assertion failures/skips. Eight are the original B migration
errors, plus the legacy direct stage_apply caller now missing its mandatory plan/
Registry binding. These are caused by the new interfaces, **not preexisting
baseline failures**. Do not claim the complete suite is green. Main must migrate these exact
legacy expectations/tests (B does not edit other owners' tests):

- test_audit: test_audit_dispatch_does_not_build_or_deploy,
  test_unreachable_not_audited (explicit Registry, canonical IDs, identity mock);
- test_delta_release: test_cached_known_host_and_failed_override_never_saved
  (override must now fail; observations cannot store address),
  test_fake_plan_apply_and_already_installed_skip,
  test_offline_plan_no_network_and_only_selected (Registry and identity mocks),
  test_staging_command_is_registered_password_free_and_no_remote_tty
  (direct stage_apply now needs registry and bound accepted plan);
- test_fleet: test_target_and_address_validation (canonical IDs, reject alias);
- test_online: test_cli_fake_transport_plan_apply_and_verify,
  test_fake_ssh_cli_code_components_verify_vertical (Registry and identity mocks).

No deployment/network/device test was run. Full host runnable/live/production
migration gates remain main/Astra responsibilities.
