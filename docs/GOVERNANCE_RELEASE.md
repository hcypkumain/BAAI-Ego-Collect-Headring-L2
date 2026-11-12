# Governance host release candidate

## Status and scope

This is a **non-production-approved host-only candidate**. A/B/C approved modules
are integrated; main's metrics/imports caller corrections and A's reviewed Handoff
rsync correction are included. All three recorded semantic caller gaps are closed
in local tests; independent combined final G2 review remains pending.
No live G3, deployment, publication or production administration is
authorized by this guide. Green local tests do not authorize those actions.

The host monitor package runs independently of the original QC service. It does
not instantiate its Queue/History database, import live Q code, install a service,
start recording, upload, apply maintenance or change network/time settings.
The legacy fleet UI/broker/import tools are **not** bundled monitor features:
their apply/import operations can mutate devices and require separate approval.

## Build and isolated installation

Use a reviewed full immutable Git commit, not a mutable working tree. Replace
`REVIEWED_COMMIT` and choose a new, empty output location:

```sh
python3 tools/build_governance_release.py --ref REVIEWED_COMMIT --output /new/build-output
```

The builder reads an explicit Git source allowlist and creates `payload.tar.gz`,
`BOM.json`, `release.json` and `router.json`. It refuses existing outputs and unsafe
paths. Build twice at the same commit and compare all four bytes/hashes. Extract
only validated regular archive members to a fresh isolated directory; never
overlay the original service, frozen WIP, production installation or older export.
No pip install, system service or production database is needed for the monitor.

`BOM.json` records the immutable build commit, exact selected source hashes and
component file sets. The derived release envelope avoids self-referential commit
hash rewriting. Artifact SHA256 values and final test counts belong to the receipt
for that exact commit, not fixed numbers in this guide. Old receipts must not be
used to certify changed sources. Final G2 reruns all suites, builds twice and tests
a new clean export from one immutable final commit; its receipts certify that
commit only.

## Monitor CLI and permission gate

Tested runtime: **CPython 3.14.4 on Linux**; the CLI rejects other Python versions.
Run from the isolated export with an empty PYTHONPATH:

```sh
PYTHONPATH= python3 -S -m host.qc.fleet_monitor.governance_cli --help
PYTHONPATH= python3 -S -m host.qc.fleet_monitor.governance_cli \
  --registry /reviewed/device-registry.json --targets cp_0042 \
  --policy /reviewed/probe-policy.json --once --format json
```

The second command is a command shape, **not authorization to contact that target**.
Only main may authorize a real read-only window after final G2. Inputs must be
explicit, strict and reviewed; the shipped example is disabled, not live inventory.
Missing/duplicate/unselected targets reject before probe processes. Enrollment and
monitor permission come from the same Registry digest; receive/standardize/upload
permissions are separate. Receive permission never implies upload authority.

The `--format` choice currently accepts **json only**, not text. Software identity, known-host policy,
OpenSSH version/account startup and exact device paths require main review. Do not
relax host-key checks to make a fixture or device pass. No real private identity or
trust configuration is read by the local fake-SSH acceptance tests.

|Exit|Meaning|
|---|---|
|0|Observation completed without warning; not production approval|
|2|CLI/input/target selection rejected|
|3|Runtime/build/adapter/cleanup prerequisite rejected|
|4|Observation failed; sensitive internals suppressed|
|5|Partial evidence or quarantine warning; inspect structured warning codes|
|130|Interrupted|

## Evidence, failures and rollback

Source `verified_files` proves only the selected source file set. Installed file
evidence requires the exact reviewed policy absolute path and BOM-relative file
binding. Default public `probe_path_bindings` is empty. Equal hash or filename
alone never establishes identity. Runtime/loaded bytes, controller state, freshness
and unmeasured components remain **unknown**, not false success. Nine component
records are retained; the host package supplies monitor source evidence only.
Disk probing defaults off. The converter, SDK, video tooling and catalog
publication remain external/unbundled/unverified; this is not complete production
QC or conversion-pipeline migration.

Actual candidate approval stays `candidate`. B's production approval enum excludes
it and returns `malformed_or_missing_resource`; that coarse code does not imply
missing packaged bytes. Test-only unknown-approval views exercise downstream BOM
validation but never approve or rewrite the real artifact.

Timeout/quarantine or unreaped cleanup produces nonzero status and
`cleanup_required` / `NO_AUTOMATIC_RETRY_REVIEW_REMOTE_CLEANUP`. Quarantine is
**process-local, not persistent across CLI invocations**. Never automatically
restart a timed-out target, even after local reap or in a fresh process. Main must
review remote cleanup before any future attempt. Local SIGKILL/SIGALRM cannot
guarantee termination of a remote kernel D-state reader. Preserve logs and stop;
do not repeatedly probe to obtain a green result.

Rollback means stop using this isolated candidate and retain its receipts. Since
the monitor performs no deployment or service replacement, there is no remote
rollback command. Do not delete/overwrite certified code, production data, keys,
retention records or one-shot import journals as a rollback workaround.

## Legacy caller boundaries and verification

Main-owned input loading and Jobs/Broker inject the same in-memory Registry as
`config['_registry']`; it is not serialized into JSON state. Reviewed
`web_config.setup` stays a pure public-config adapter. Six metrics SSH helpers and
the USB remote helper use the existing `fleet.registry_ssh` identity/trust builder,
require monitor enrollment and explicit selection, and reject config drift before
transport. Metrics plans bind digest/Device identity before hashing/writing.

USB scan records and preview plans carry the same binding. Preview/execute reject
missing, wrong or stale identities before remote action or one-shot marker writes.
The original one-shot journal, ZIP/privacy/controller/revision guards and
`remove_list_ids=[]` preservation remain in force. `imports.REGISTRY='usb-tasks-v1'`
is a protocol constant, not device enrollment. Historical cp0018 TEST audit roots
remain explicitly historical evidence scope, not a general today's-device promise.

Local caller state and ordinary/streaming IPC use frozen C0 strict JSON parsing:
duplicate keys, NaN/Infinity and overflowing JSON numbers reject. State reads are
bounded regular non-symlink files; execute parses and hashes the same bytes.
Timestamps must be finite non-boolean numbers before the existing 600-second
expiry check (no new future-time policy). Canonical valid JSON retains its prior
encoding/newline/hash; unknown remote reason strings are replaced by fixed codes.

Run all `test_*.py` suites under `host/common/tests`, `host/qc/tests`,
`services/headring-qc/tests`, `host/standardizer/tests` and
`tools/fleet_maintenance/tests`; run source scope lint as well. Reports distinguish
unique unittest IDs from repeated method implementations. Local tests include
real HMAC, temporary DBs, fixed-reader child/IPC and actual B validation. Fake SSH
tests execute local processes, **not** cryptographic SSH/device trust validation.
No local result constitutes G3 or production approval.

Historical checkpoints and exact commit-specific evidence are external handoff
reports under `/tmp/opencode/8788-sol-governance-*.md/json`; they are not alternative
runtime instructions. The final local combined receipt is
`/tmp/opencode/8788-sol-governance-G2-final.md/json`; the earlier caller receipt
certifies its own intermediate commit, not the final package. Independent main
signoff is not implied by either local receipt. START and impedance were neither
fixed nor hardware-validated in this candidate; raw/disk scans are not performed.
