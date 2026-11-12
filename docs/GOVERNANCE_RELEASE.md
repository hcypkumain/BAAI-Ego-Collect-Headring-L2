# Governance host candidate — integrated local evidence, independent G2 pending

## Current consolidated checkpoint (supersedes historical premerge notes below)

All three G1-approved original+fix pairs have been integrated in main-authorized
order, with immutable base/allowlist/shared-interface checks and no conflicts:
A `a63199f` + `2aed528`, B `4f2c6e4` + `1f9f96b`, C `31791b8` + `5b188d7`.
Main's M1 metadata/verified_files repair is `b71bfc6`; service caller migration is
`93bb4c2`, fleet caller/test migration `cfc5c38`. C0 contracts/schema/factory remain
unchanged. The source-provenance selection update intentionally preserves original
upstream hashes plus C0 decisions and separately records immutable A artifacts.

The actual full service suite passes 71/71 (all eight constructor errors closed),
standardizer 10/10, fleet 157/157 (all nine new migration errors closed), common
16/16, full host/qc 76/76. These path-disjoint suites total **330 unittest IDs**,
not repeated counts of C0 or tests invoked through imports. The host/qc
total includes 37 main-owned tests, 28 real C tests and 11 older regressions.
These are 330 unique unittest IDs and **326 unique method implementations**:
four intentional A constructor-wrapper cases repeat the four original Handoff
methods. C0 is counted once, not once per module run. Counts are command outputs
at this checkpoint; final report records the final run and implementation IDs.

Real integration tests use actual HMAC verification, actual temporary Store and
Handoff discovery, real C local child domains/fixed reader/strict IPC, actual B
build_bom/compare/validator and actual CLI rendering. Six namespaces share the
legal session/content but retain device/generation/digest isolation; original four
upload grants remain independent and 42 denied. Wrong identity/digest/device/
generation, incorrect file labels, unknown runtime, real timeout/quarantine and
false-close cleanup are exercised. Existing actual two-hung/third-three-round and
100-timeout resource tests are retained; mock cancellation is not substituted.

Main-reviewed absolute-to-logical file bindings have an exact three-field record
(`component_id`, `absolute_path`, `relative_path`), require the identical C0 policy
path and BOM source file, and rewrite **installed** labels only. Public candidate
manifest defaults to an empty binding list, so unreviewed mappings stay unknown.
Manifest bytes are read once, parsed and hash-checked against the same BOM envelope;
no second-read identity drift. Runtime labels are never manufactured. B's boolean
matches_source is shown as file evidence only, not whole-component verification.

Timeout/quarantine and unreaped close output `cleanup_required=true` and fixed
`NO_AUTOMATIC_RETRY_REVIEW_REMOTE_CLEANUP`, with nonzero exit. Strong references
deny same-process rebuild after false close; C's tombstones also deny a new
scheduler after local reap. **This quarantine is process-wide, not persistent
across CLI processes. Never automatically restart a timed-out target in a new
CLI process.** Operator/main must review remote cleanup before any future attempt;
G3 is one explicit attempt and never retries that target on timeout. No temporary
persistent-gate framework or uninterruptible-kernel kill guarantee is invented.

Two exact legacy-adapter scope issues remain registered pending main/owner
allocation: Handoff rsync still uses its reviewed old transport builder, and
metrics.create_plan/its SSH callers still lack Registry injection. Discovery and
governance host execution do not prove those legacy paths complete. These do not
justify restoring defaults or silently monkeypatching core/metrics. Independent
G2 cannot be claimed complete until resolved or explicitly excluded by main.

The host-only immutable package and its two real builds/clean parser/synthetic-SSH
execution receipts are recorded in the external final G2 result, not guessed by
this document. No real SSH/trust/key/production config/HTTP/DB or device operation
is used. Synthetic SSH admission/process execution is **not** cryptographic SSH
trust or live verification. Host candidate runtime capability, production
approval and production pipeline migration remain separate acceptance claims.

B's production approval enum intentionally accepts approved/unknown/rejected,
not the host package's `candidate`. Therefore validating the actual candidate
returns the coarse blocking code `malformed_or_missing_resource`; do not describe
that as missing packaged bytes or approval. An explicitly test-only **unknown**
approval view runs through all remaining real validator gates: valid nine-component
shape is rejected only for missing approvals, while phone/helper omission, router
tamper and source resource tamper add their exact blocking codes. The real
candidate files are never rewritten or self-approved to obtain an empty tuple.

## Historical premerge and implementation detail

## M1 review repair and A integration checkpoint

M1 renderer now preserves B's actual `verified_files` source state and valid
file-set hashes (unknown or malformed hashes stay unknown); it never promotes
source to installed/runtime. Candidate integrity reconstructs the exact release
key set and every value from the hashed embedded manifest and BOM build_commit.
Tests first reproduced the two failures, then verify all metadata mutations,
including coordinated release_id/router change and unknown fields, are rejected.
Candidate integrity is not production approval.

A original plus expiry fix are integrated only after explicit main permission.
Original standalone service suite's eight newly introduced constructor errors
were reproduced, then closed by **only** injecting shared fixture Registry inputs
in test_receiver_http/test_handoff_receiver; original assertions/authentication/
QC/deletion/retention behavior remain unchanged. Service configuration and agent
source loading moved to explicit main arguments; module import/setup do not read
live config, open DB, create workers or connect. `--registry`, `--config`,
`--identity-file`, `--known-hosts` are required. Minimal root import setup is owned
by the application, not a live Q path.

Service DEVICES/CONFIG.devices derive from enrolled receive members in the same
Registry, including SSH alias/address/user/host-key alias. Legacy lists contribute
only restrictive per-device cutoff/history metadata, never membership/identity.
Adding 42 to that one registry populates actual Handoff scanner definitions with
no second-list edit. RPC SSH uses registry identity directly, `/dev/null` config,
explicit software identity and strict pinned known-host options. These are code
changes only, not real trust admission or production migration.

**Additional G2 blocker registered, not silently patched in A core:** Handoff's
reviewed `_transfer` still hardcodes the legacy SSH config and canonical device ID
for rsync. Service RPC's registry alias is therefore not sufficient to prove the
complete arbitrary-alias data transfer path. Main/Astra must route this minimal
adapter fix to A or explicitly approve its scope; this owner leaves reviewed A
core unchanged. Current main tests prove actual Handoff discovery/enqueue, real
signed standardizer verification and upload denial, **not** completed rsync/QC
conversion for a newly aliased device.

Lint exceptions now require owner/reason/reviewed_at/expiry as well as exact
path/rule/symbol/source hash. Expired exceptions do not suppress findings. Metadata
is validated before any allowed source is opened; excluded decoy reads remain zero.
Public provenance retains original source SHA and C0 artifact/selection history,
and separately records selected A artifact hashes/ref. No hash claims installation
or runtime. Converter/SDK/video tools/catalog remain unbundled and unverified.

B's newly approved fix is queued for a separate authorized merge/caller checkpoint;
its **nine** legacy migration errors (original eight plus direct stage_apply)
must all close. C remains unmerged/blocked. Full G2/G3/host-runnable readiness is
still false; this checkpoint does not relax any gate below.

This release is a **candidate**, host-only/read-only. The original services,
stable refs, frozen WIP and real inventory/trust are unchanged. No START/impedance
repair, production pipeline migration, live read, deployment or device writes are
claimed. All A/B/C branches start at the independently approved C0 commit
`1404ba567009058e40d4294f8555a66e5ee8ca86`; shared contracts/schema/fixtures and
source provenance remain frozen while workers run.

## CLI

```sh
python3 -m host.qc.fleet_monitor.governance_cli --help
python3 -m host.qc.fleet_monitor.governance_cli --registry <explicit-local-json> --targets <comma-separated-ids> --policy <explicit-local-json> --once --format json
```

Only the first command is safe to run without an independently reviewed readonly
window after final integration. This development round uses **no real SSH, HTTP,
DB, private trust/keys/config, device connections or services**. Do not use a live
registry/policy merely to test this command. Tests use synthetic inputs only.

Help/missing/duplicate/malformed targets read no files and start no adapters.
Registry/policy are explicit bounded (1 MiB) JSON inputs, decoded by C0's duplicate
key/nonfinite-rejecting parser. Selection uses C0 monitor gates before policy or
adapter loading; unknown/disabled targets cannot create processes. No fallback
inventory, inferred target, mutable cache, legacy Store/Queue/history, config
scanner, UI SSH, HTTP server, watch mode or automatic retries are introduced.

One invocation is one locally owned generation (initial generation 1). The
scheduler accepts only matching registry digest, selected device ID and generation;
duplicates, unknown IDs, wrong digests and late generations are rejected. C's
initial generation convention must be confirmed at merge; no shared API is changed
to guess it. Per-device result collection is bounded by explicit fleet size and
policy time budget; `close(timeout_s=2.0)` runs in `finally`, including interrupt.
Unreaped close fails with `CLOSE_NOT_REAPED`, never success. C remains responsible
for actual process/SSH isolation and remote cancellation limits; fake scheduler
tests do **not** prove that behavior. Empty selection never creates a scheduler.
CLI holds C's coalesced staged result without draining until all selected attempts
reach their terminal/quarantine boundary (or bounded request deadline). Thus it
does not truncate staged RAM before later evidence or accidentally start generation
2 by draining early. Tick interval is 10 ms; TERM/KILL grace stays C-owned. Stats
are strict bounded integers; only reviewed counters are serialized. False close
retains a strong scheduler reference and denies any new observe call in that
process, rather than discarding the ledger or auto-retrying. Partial/unknown
evidence, quarantine, rejected results or deadline emit fixed warning codes;
completed partial observations return exit **5**, not a green success.

C's fixed docs at `31791b887d0920522311e6c61cdfe3771dd41212` were read without
merging or reading WIP. Admission is only exact Host alias plus one IdentityFile;
every other SSH directive is rejected. SSH uses `-F /dev/null`, not evaluation of
that file. Main must later create its own small local-only admission file, approve
exact pinned software host keys/noninteractive software identity key/account
startup and fixed Python/OpenSSH. Never give C an existing broad config and relax
its parser. This CLI round reads no trust or keys and makes no real SSH calls.

Output keeps controller unknown unless evidence supplies a recognized explicit
state. No PID-to-idle/stopped inference or installed-to-runtime substitution.
`value_json` is strictly decoded for presentation. Only fixed public source labels
are echoed; other adapter/path labels are withheld. Raw adapter reasons/stderr,
exceptions and comparison dictionaries are not printed. Canonical UUID boot IDs,
strict integer RAM/disk and C0-validated component hashes are displayed; unreviewed
controller strings become unknown rather than echoing arbitrary private content.
Sample age/freshness remain null/unknown: device wall-clock timestamps or receipt
time cannot prove sample age. No device wall-clock synchronization changes occur.

**Premerge gaps, intentionally visible:** missing B/C produces
`ADAPTER_NOT_INTEGRATED`; missing built BOM produces `RELEASE_NOT_BUILT`.
B's fixed delivery documentation at commit
`4f2c6e4ecebcf2ab390e60cdc891dbc3afdf4f67` has been read without reading WIP or
cherry-picking. `compare_components(bom, result)` returns a full copied BOM;
presentation keeps the nine IDs and only typed hash/status columns, labels source
as selected-file-set and installed/runtime as file-evidence-only, and withholds
raw callback content. It never calls a file hash whole-component loaded proof.
Installed comparisons require exact `file:<BOM-relative-path>` and runtime requires
independent `loaded:file:<same-path>` evidence according to B. No absolute policy
path is automatically relabelled: until main approves that binding after C merges,
mismatches stay unknown. Scheduler location currently follows `isolated_transport.ProbeScheduler`
and must be checked against C's delivery docs (no fallback shim). The CLI does not
run the production adapter until all independent gates pass.

## Immutable host-only builder

```sh
python3 tools/build_governance_release.py --output <new-isolated-directory>
python3 tools/build_governance_release.py --ref <immutable-commit> --output <another-new-directory>
```

The parent must already exist; output must not exist. All ancestor traversal is
opened with no-symlink directory descriptors; final files use exclusive creation.
No overwrite, recursive delete, system installation, service operation or network
fetch. Partial isolated output is retained on write failure for inspection, not
automatically deleted. The builder reads exactly its code-owned `PAYLOAD_PATHS`
from one resolved Git commit and checks Git file modes (no symlinks). It does not
copy a whole worktree, source backups, state, venv, raw/image/data, private registry,
trust, keys, converter or SDK. No arbitrary manifest path may expand that list.

The immutable `deployment/manifests/governance-release.json` must match the code
allowlist. Required B/C files missing in the selected commit cause a clear failure
before output creation; this is expected in premerge, **not a successful build**.
The executing Python payload must match selected immutable bytes before importing
B's BOM implementation. No dependency installation or live source import occurs.

Build outputs:

- `payload.tar.gz`: sorted USTAR members, mtime/uid/gid=0, mode 0644, empty owner
  names; gzip filename empty and mtime=0. Python modules run with `python -m`.
- `BOM.json`: exact build commit, file hashes/sizes, B component BOM, exact CPython
  version, unknown/uninvoked OpenSSH version, candidate approval. The identical BOM
  is inside the payload. File source hashes are **not** installed/runtime proof.
- `release.json` and `router.json`: one **local candidate** release/resource digest
  binding to the complete resource map (including payload and BOM). They do not modify production routing.
  `verify_candidate` rejects mismatched router/resource/BOM/payload members. This
  is integrity checking, not production approval. The builder additionally calls
  B's actual validator and reports its blocking codes without suppressing them.
  Integrity validation reconstructs every exact release field from the hashed
  embedded manifest and BOM build_commit; unknown fields and metadata mutations
  (even a release ID changed together with router) are rejected. Source output
  preserves B's `verified_files` file-set status/hash, never installation/runtime
  or whole-component approval. Integrity is not a signature or production approval.

Canonical payload/BOM bytes reproduce for the same immutable inputs, BOM API and
exact Python runtime. Build/observation are locked to tested **CPython 3.14.4** on
Linux (POSIX openat/O_NOFOLLOW); other versions require explicit main revalidation.
Help/argument rejection remain usable without invoking adapters. Runtime package needs standard library governance
modules; OpenSSH executable/version/strict argv/trust are a later reviewed C/main
requirement. No claim is made that any OpenSSH version has been live-verified.
Pipeline converter, SDK and private config remain explicitly unbundled/unverified.

Builder inputs now use B's exact nine-component source_manifest: schema_version,
base_commit, components with exact component_id/files/approval; each file has
immutable ref/path/sha256/provenance/resource. Only the host governance binary's
`monitor` file set is declared and in scope, all eight other component sources stay
explicitly unknown. Pending component approvals are unknown; release approval is
candidate, never approved. The host-only callback exposes precisely those selected
immutable host files. No device collector/phone/helper payload is silently added.
The frozen upstream source-provenance manifest stays separate and unchanged; host
binary file labels use public `Git/host-governance/...` source labels.

The release uses B's exact release fields and `resource_digest(resources)`; router
maps release_id to that **canonical resource-map digest**, not tar SHA256. Payload
SHA256 and BOM SHA256 remain separately reported. Production approval validation
must remain blocking for this candidate. Base+collector overlay reconstruction
stays B's independent API/test obligation, not a claim of this host-only payload.

Main must check actual B/C import closure and reviewed evidence-path binding, then build twice from
a clean merged commit and run clean-package help, missing-target rejection and
synthetic SSH tests. The current pure assembly tests use labeled synthetic B/C
bytes/BOM callbacks, not a full nine-component release or converted dataset.
Approval stays `candidate`; no flag makes it production-approved.

## Path-first generality lint

```sh
python3 tools/check_device_generality.py --scope tools/source_scan_scope.json
```

This audits only the exact governance-critical paths enumerated in the scope. It
does **not** replace Astra rules, secret audit, or claim entire-repository coverage.
The full scope is enumerated/validated before any source content read. State,
runtime, venv/venv-runtime, generated integrations, private/secrets/keys, image,
raw/data/logs and any symlink ancestor are excluded before opening. Reads recheck
each path component with `O_NOFOLLOW`; sources are bounded regular text files.
No recursive glob/scandir traversal is performed.

AST rules catch concrete device comparisons, code/generated device lists,
device-specific function/argparse defaults, and non-loopback IP literals in
runtime code. Output includes paths/rule/line/symbol, **never literal values**.
Nonexecuted docs/config are explicitly categorized. Fixture/history exceptions
and legacy debts require exact path/rule/symbol/category/reason plus exact source
SHA256; a code change invalidates the exception rather than hiding a new list.
No wildcard service-wide exemption or automatic guard deletion is performed.

Certified premerge debt is explicit in the scope: receiver TARGETS; fleet main's
default targets; delta DEFAULT_INVENTORY and legacy bind fallback; web_config
DEFAULT inventory/bind configuration. A/B must remove/replace authority within
their allowlists; surviving changed-source debt blocks lint until main reviews it.
Signature, serial, retention and safety guards are not mechanically removed.
Missing B/C source is reported `NOT_INTEGRATED` and `integration_complete=false`;
lint can pass its current-source findings without implying release readiness.

Tests create state/venv-runtime/private/generated/image/raw decoys and a key
symlink; all have **zero content reads**. One exact allowed synthetic source is
opened. Invalid later scope entries prevent even that earlier read.

## Local tests and remaining real G2

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s host/common/tests -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s host/qc/tests -p 'test_governance_integration.py'
git diff --check
```

The main-owned integration test file currently tests CLI validation/binding/
interrupt-close with explicit C0 mocks, path-first lint with adversarial synthetic
files, and deterministic builder/integrity/no-overwrite behavior. It also exports
real main/C0 parser files with **explicitly fake B/C adapters** and proves clean
package help/missing-target behavior using `python -S` with empty PYTHONPATH.
There are no skip-to-ready tests. These are **premerge unit tests**, not final G2.

After main receives authorized worker commits, it must first validate base/diffs/
allowlists/hashes, cherry-pick without automatic conflict resolution, and adapt
service.py only after A. Then extend this same main-owned test file with:

1. Actual temporary ReceiverStore and purified standardizer receipt verification
   using real synthetic signatures; only registry config adds 42; monitor digest
   matches; 42 upload denied; actual four grants preserved. Converter stub proves
   handoff only, not real format conversion.
2. Five+42 same legal session `L3-2026-11-13-40177`, device/generation isolation,
   mixed content labels/etag fixtures, unknown 43/wrong identity rejected,
   busy/offline never invented as stopped; WARN/completed/ledger semantics intact.
3. Real B full nine-component BOM, missing phone/helper, router/resource tamper,
   base+approved overlay, unknown runtime never green; no cp15 unapproved release.
4. Actual C local synthetic TERM-ignoring subprocess tests: two hung workers,
   third served within 8 seconds and three rounds, unreaped quarantine and
   bounded resource accounting. No device fault injection or real SSH.
5. Real clean merged package twice, actual B/C imports and synthetic SSH transport;
   validate stdout against actual B output, immutable BOM sources and candidate validation.

### Additional B caller/test migration gates (new errors, not baseline debt)

B reported C0's original fleet suite **107/107 passed**; after its interface change
the full suite is 145 run / 137 passed / **8 new unmigrated errors**. These are
release-blocking migration work, not pre-existing baseline failures. Main owns
the following after Astra G1 approves B and main explicitly permits merge:

- web.py main loads one explicit strict registry/targets/transport input and calls
  pure web_config.setup(registry=..., selected=..., config=...).
- broker.py main/handle retain that same Registry object/digest/selection/config,
  recheck identity before authorization; broker.AuthSSH keeps original auth.
- jobs.Jobs, operations.execute and UI command builders propagate identities and
  explicit --registry/--targets; no observed cache can change address/identity.
- No catch-TypeError compatibility fallback, no Q default config, alias inventory
  fallback, weakened idle/auth/approval/pin/baseline checks or production GUI run.
- Update synthetic expectations while preserving assertions in test_audit
  (audit dispatch, unreachable), test_delta_release (cached host/override denial,
  fake plan/apply skip, explicit offline selection), test_fleet (canonical target
  and address), and test_online (fake transport plan/apply/verify and code-component
  vertical). All eight exact test IDs are in B's fixed RELEASE_VALIDATION.md.

These caller/test changes are deliberately **not made before G1 merge permission**.
Service.py adaptation still waits for A merge permission. Shared C0 interfaces remain frozen; any
needed change requires main-approved C1 rather than a private shim.

A's fixed doc at `a63199f53a4bef11a1e5bf4540256912a5d74ff2` was read without
merging or reading WIP. Main's additional assigned plan is to load one explicit
strict registry at service.py's entrypoint and pass that same object to
HandoffReceiver/Store, reconcile legacy connection metadata with its Device
identities without union-enrolling transport entries, and adapt only constructor
inputs in original test_receiver_http/test_handoff_receiver with shared synthetic
fixtures. Run the original standalone suites, preserving real signatures/QC and
substantive assertions, not just A's injected wrapper suite. No production service
or DB migration is run. The purified standardizer removes the implicit legacy
catalog subprocess because it loaded another config: **catalog publication remains
external/unverified**, as do converter/venv/video tools/SDK. Host-only governance
payload does not include a standalone runnable data pipeline or prove conversion.
After merge permission main may refresh only selected artifact hashes in public
provenance, preserving original reviewed source hashes and selection history;
neither is installed/runtime evidence.

Only after independent G2 cancellation/identity/release review may **main** use
an explicitly authorized single real target/window for read-only G3. Workers and
this round never connect. No recording, fault injection, data deletion, batch
deploy, network/time/route/service change or private configuration upload.

Current flags: `code_passed` refers to premerge tests only;
`host_runnable=false`, `live_read_verified=false`,
`production_pipeline_migrated=false`. Exit the independent foreground host CLI to
stop it; original services do not require rollback because they were not changed.
