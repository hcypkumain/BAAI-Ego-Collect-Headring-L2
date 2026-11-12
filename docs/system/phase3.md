# Phase 3: offline read-model vertical slice

Review status: Astra found R1/R2 high and R3 medium in `ae60c5c`; the minimal
corrections are documented in `REVIEW_FIXES.md`. Independent re-review/signoff
is pending. Phase4A has not started.

**Not deployed. No device, production HTTP, database, service, route or network
configuration was changed.** Existing controller actions, impedance controls,
WARN completion rules, ledger, task grouping and USB transport are unchanged.

## Source / design archive

- `phase2/`: complete phase2 prose and evidence index, with private host paths
  replaced by logical R/C/Q/P1/PHONE references. Original Documents files remain
  untouched. Original SHA256 values and destination paths are in
  `phase2/ARCHIVE_MANIFEST.json`; these are hashes **before** redaction/corrections.
- `contracts/v1/` from repository root: all four schemas,
  original 25 positive/negative cases, examples and offline validator. Prose
  references to `schemas/`, `fixtures/`, `validate_examples.py` mean that directory.
- `host/qc/SOURCE_MANIFEST.json`: six explicitly allowlisted Q source files,
  original hashes and sizes (128387 bytes total). No configuration, raw data,
  private recovery workspace, keys, database, .git, venv, log or media copied.
  Q is a logical source root; private absolute source locations stay off GitHub.
  This directory is a source archive, **not a second deployed service**.

### Consistency review / minimal corrections

Main's corrected policy in CONTRACTS/README/DECISIONS takes precedence over stale
machine-readable remnants. Archived schema now permits `can_review=null`;
the validator no longer requires fresh monitoring samples for a completed review
gate, nor discards independent known ledger counts merely because a plan policy
is missing. EVIDENCE U07 no longer recommends changing WARN to pending. Its other
findings remain historical evidence, not phase3 runtime claims. The original
example's explicit `NOT_MEASURED` false remains a synthetic denial example;
the actual unknown builder emits null, never a false assertion without evidence.
No new completion policy, grouping default or measurement timeout was introduced.

The archived recovery adapter's private absolute workspace and volume identity
are redacted. Its import is lazy in legacy Hub construction; the v1 path never
imports it. Other archived legacy source remains legacy and is **not** a v1
fallback. In particular agent/thumbnail are not safe generic import targets.

## Implemented chain and activation boundary

`SnapshotPublisher.publish(explicit cached evidence)` → preencoded immutable
pointer → real Dashboard nested handler `/api/v1/snapshot` (existing auth,
query forbidden, typed 503 before first publish) → `Hub.read_only(ReadModel)` →
`/api/v1/fleet` and `/api/v1/fleet/events` → shared `host/qc/fleet-v1.js` renderer
in actual receiver.html and actual maintenance app.js.

The controller accepts an explicitly injected publisher. Its dashboard lifecycle
resets the publisher to a new controller UUID; existing dashboard publication
passes only cached mode and explicit worker recording ACK. Missing identities,
scope, resources, health, policy and version evidence stay null/unknown. There
is no sampler or low-frequency thread. No START/STOP/journal/receipt path exists.
Default production Controller has no publisher: new GET returns typed 503.

The host has no v1 network poller. Observation tickets are allocated locally
before external work; verified identity and host-issued ordering are required to
change instance. Retired instances cannot reclaim a row, and same-instance old
seq is rejected. `snapshot_age_ms` must be supplied from the device response age
header plus bounded transport elapsed; absent age yields stale/unknown, not
fresh. Receiving HTTP/304 is not a controller publish. All cross-machine age
uses host elapsed plus reported age, never subtraction of two machine clocks.
Restored host rows have stale snapshots, null age and unknown connectivity.
Offline does not overwrite recording facts. Pipeline source failure does not
scan raw. Initial pipeline/storage stay unknown, deletion remains unauthorized.

v1 SSE carries host_instance:revision and reset notifications. Subscribers
cannot start telemetry; gaps/overflow require a full pure GET. The fixture UI
uses full pure GET, not the legacy SSE/summary routes. The renderer advances age
locally without renewing anything; unknown is never zero or a green ready state.

Workbench's same-origin `/api/v1/fleet` accepts only an explicitly injected
in-process provider, identical to the Hub's model. It has no URL configuration,
proxy, remote fetch, browser token or second truth cache. By default it returns
503. Existing maintenance jobs/operations remain separate point-in-time reports;
the new view never invokes refresh/check/SSH. Existing production GUI and broker
processes were not restarted or reconfigured.

## Local synthetic demonstration

From repository root, using an existing Python environment with jsonschema and
referencing (no dependencies installed by this work):

```sh
python3 contracts/v1/validate_examples.py
python3 tools/system_read_model_fixture.py --output /tmp/opencode/system-v1-fixture.json
node host/qc/tests/test_fleet_cards.cjs /tmp/opencode/system-v1-fixture.json
python3 tools/system_read_model_fixture.py --serve
```

The last command binds **only 127.0.0.1 on an ephemeral port**, prints its URL,
and serves `/receiver.html?v1=1` plus the workbench `/` (explicit v1 button).
Five entirely synthetic cards demonstrate unknown/known/stale/error/reboot.
Do not point this at production ports or present it as five live devices.
An exported host fixture can also be selected using receiver's fixture file
input; it never opens device connections. Ctrl-C closes the fixture server.

## Shadow evidence / tests

`contracts/v1/fixtures/shadow-phase1.json` is a redacted inherited summary, not
an uploaded device response: cp15 EEG, approximately 55h clock gap, invalid
calibration, missing grouping policy; four other devices unknown. Legacy
inferences are explicitly synthetic. `shadow-diff.json` distinguishes known
consistency, missing unknown, discarded inferences and clock conflict. The WARN
ledger case is explicitly synthetic and not a claim about cp15 counts.

Tests exercise actual Dashboard handler code via AST extraction/dependency
injection (no controller constructor or hardware imports), local HTTP and 304,
actual Hub and maintenance handler against one model, actual shared JS renderer
and both real consumer entry functions. IO/process/statvfs/thread/outbound
socket/History spies fail reads; only local HTTP infrastructure is allowed.
SSE reset and bounded overflow, lifecycle reset, stale gates, duplicate keys,
NaN, secrets as unexpected fields, boot/seq/count errors are covered.

Exact commands and baseline/current results are recorded in the delivery report.
No hardware, cloud, mobile browser, production authentication or performance
acceptance is claimed. Baseline source is extracted only into an approved temp
directory, never switched into a protected worktree.

## Limits and phase 4A handoff

- jsonschema/referencing and contracts must be packaged with the optional v1
  publisher; production default does not import that optional publisher. No
  dependency installation or production enablement occurred here.
- No persistent projection or live poller; memory event/ticket/retirement
  budgets fail closed. Future transport needs authenticated device identity,
  one in-flight request/device, cancellation/release of failed tickets, ETag
  age handling and fixed registered targets. No legacy fallback is allowed.
- Source/install/runtime/approval remain independent. Cached historical
  measurements are not current device authorization; UI has no control action.
- 4A should package contracts/dependencies, add explicit cached authoritative
  events, then bounded RAM/disk samplers with a single non-cancellable disk IO
  in flight. Add controlled host polling and projection persistence separately.
  All remain offline until the main owner grants phase5 single-device windows.
- Rollback is leaving the optional injection/view disabled. No production
  service needs rollback because none was changed.
