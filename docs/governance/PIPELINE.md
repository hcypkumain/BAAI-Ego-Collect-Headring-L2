# Registry-injected pipeline (A)

This adapter consumes frozen C0 `docs/governance/CONTRACT.md`. Enrollment is
not upload authorization. No production inventory, credentials or data was read
or migrated; all verification below uses temporary synthetic data.

## APIs and authorization

- `ReceiverStore(root, clock=time.time, *, registry)` requires the caller's
  validated Registry. `enqueue`, device records and claims use its enrolled AND
  receive-member set. Existing persisted jobs outside that set cannot be claimed.
  Device/session is the durable identity, including identical session names on
  different devices. Native-content conflict, STOP/CAS, authentication, recovery
  and retention semantics remain in place. Same-device receive/release/reply I/O
  claims are serialized in the SQLite transaction; another device can progress.
  As before, startup recovery requires a single service owner: do not initialize
  another owner against an actively running ledger (initialization recovers
  `running` jobs after restart).
- `HandoffReceiver(legacy, *, registry)` passes the identical object to Store,
  builds locks from `receive_ids`, and filters legacy connection definitions by
  that same set. It does not enroll legacy devices. Connection definitions must
  already exist in the explicit legacy transport config; registry-only Store
  enrollment does not manufacture legacy SSH configuration. Certified source-QC
  binding, portable signatures, required-device v2, archive verification and
  deletion guards are retained, not replaced by the weaker live source.
- `make_context(*, registry, config, qc_root, data_root, state_root, log_root)`
  creates a frozen `WorkerContext` without application I/O. Config is a canonical
  immutable JSON snapshot; its data root must match the explicit data root.
  `verify_receipt(path, *, context)`, `make_one(path, force=False, *, context)`,
  `snapshot(*, context)` and `run_pass(..., *, context)` share that context.
  Receipt HMAC/server/purpose, archive device/session path, checksums and link
  guards are real. Storage relocation and UUID/mount/read-only/free-space guards
  remain; every storage call receives explicit config and config root.
- The worker imports no runtime config, changes no `sys.path`, and opens no DB,
  socket, thread or process at import. Only its `main()` loads CLI inputs; all six
  path/registry options are required. The legacy catalog subprocess was removed
  from `run_pass`: it loads a separate implicit config and cannot safely share
  this registry context. Catalog publication is not validated by this adapter.
- `upload_allowed(target, *, registry, now_utc, device_id=None)` validates the
  full dataset path, requires `root/device/package...`, optionally checks the
  supplied source device, and passes only the exact project root to the shared
  grant gate. Calling the helper without a source checks candidate scope only.
  Production Queue additionally binds the **entire target suffix** to the
  actual source path below `standardized`, including the source device.
- `UploadQueue(app_root, state_root, proxy, *, registry)` checks grants at seed,
  queue selection, entry, SDK batch, each file and cloud confirmation. Task-creation
  authorization is rechecked immediately before `client.begin`, after project
  binding and SDK preparation, so expiry during preparation creates no task and
  does not update the queued task ID. Synthetic negative/positive tests exercise
  the real batch method with a controlled UTC clock and in-memory task ledger.
  Snapshot counts exclude denied rows and expose `denied`; denied persisted rows prevent
  `all_confirmed`. Project binding, fixed profiles/file lists, cloud verification,
  sealed retention and no-delete behavior remain. The constructor still opens
  credentials/DB only when explicitly run; governance CLI must never construct it.

## Explicit offline migration

`tools.migrate_device_registry.migrate_legacy(legacy, *, receive_members, grants)`
returns `{registry, digest, diff}`. It never reads a default file or discovers
addresses. Each device requires explicit `id` (or `device_id`), `ssh_alias`,
`address`, `ssh_user`, `host_key_alias`; ambiguous IDs and missing trust identities
fail. Optional `enabled` and `monitor_enabled` default **false**, capabilities
default empty. Receive flags come only from the explicit selection; they do not
override disabled enrollment. Grants come only from the explicit mapping and
retain reason/owner/expiry. There is no parser or migrator four-device default.
Callers supply the historical four-device grants explicitly when appropriate.

The CLI requires `--legacy`, `--receive-members` and `--grants` JSON paths and
prints the conversion/difference envelope. All new JSON file entrypoints use
`strict_json_loads` before semantic parsing (duplicates/NaN are errors). Extract
the envelope's `registry` member before passing it to other registry consumers.
Legacy SSH-derived identity fields must be explicitly prepared by main; the
migrator will not infer trust from a hostname or inspect SSH configuration.

## Main-owned integration still required

1. `services/headring-qc/service.py:502`: load one explicit registry with strict
   JSON at the service entrypoint, then pass `registry=...` to HandoffReceiver.
   Reconcile legacy transport definitions/`DEVICES` with that same inventory;
   transport metadata is not enrollment authority. No implicit registry fallback.
2. `services/headring-qc/tests/test_receiver_http.py:11`: pass a synthetic Registry
   to Store. `services/headring-qc/tests/test_handoff_receiver.py:33`: pass a
   synthetic Registry containing its test device. A does not own these files.
   The A governance suite runs all four original handoff tests unchanged via a
   constructor-only registry injection and generated temporary signing key.
3. Explicit worker runtime invocation now needs `--registry --config --qc-root
   --data-root --state-root --log-root`. External worker callers must pass context
   to verify/make/status/pass; the archived Python converter/venv/video tools are
   unbundled external dependencies, not verified runnable payloads.
4. Explicit uploader invocation now requires `--registry`; external Queue callers
   must pass the identical registry. Private config reading remains in explicit
   runtime Queue construction, never in read-only governance orchestration.
5. Packaging must include `host.common` and `services/headring-qc/host_storage.py`
   under repository-root imports. Worker lazily imports that storage module, not
   a local/runtime config module. No additional source or SDK was copied.

## Offline regression commands

From repo root, prefix each command with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.`:

```sh
python3 -m unittest discover -s services/headring-qc/tests -p test_governance_enrollment.py -v
python3 -m unittest discover -s services/headring-qc/tests -p test_custody_state.py -v
python3 -m unittest discover -s host/standardizer/tests -p test_registry_adapter.py -v
python3 -m unittest discover -s host/common/tests -p 'test_*.py'
```

Synthetic F09 inventory is 14/15/18/20/22/42, made with main's shared factory;
the original four-grant cases also use that factory. Tests cover unknown 43,
receive revocation, same legal session across devices, transaction claim isolation,
generated-key signed archive receipts, source/device mismatch, expiry, denied
seed/claim/upload/stats and strict JSON. The converter test explicitly injects
`dataclasses.replace(context, executor=synthetic_executor)` and synthetic free-space
evidence. It validates orchestration only, **not** real conversion or format.
No actual Queue constructor, private credentials, cloud SDK, production DB,
device SSH, volume scan, service, route, ADB or upload is used by these tests.

Local module tests are not full main integration or live evidence. Production
pipeline migration, packaging/converter validation and live verification remain
false/not performed; no START, impedance or automatic recording claim is made.

## G2 handoff rsync identity correction

The handoff transfer now uses `legacy.ssh(device, '')`, the same main-owned
registry-to-SSH builder used by service RPC and service rsync. It does not build a
second SSH identity policy or read a legacy SSH config. `_transfer_argv` checks
receive authorization, matching receiver/runtime/config registry digests, legal
session and job metadata, canonical absolute paths, and the builder's expected
Registry device alias. A missing/incomplete builder, empty arguments, a nonempty
remote command, alias drift or a legacy config fallback is rejected before idle
RPC, transfer logging or Popen. No implicit canonical-device SSH target remains.

Both pull and push use the builder alias as rsync's remote host and
`shlex.join(transport[:-2])` as `-e`; opaque SSH argument quoting is preserved.
The existing `--protect-args`, partial transfer, rate limit, STOP/idle/storage,
process cleanup and timing behavior remain. Remote capture paths are canonical
absolute safe paths ending in the job's legal session; reply destinations retain
the existing device-side results root. Source-QC, signed receipts, required-device,
archive, WARN ledger, retention and deletion logic were not modified.

Synthetic tests use the real import-safe `service.ssh` after explicit
`configure_runtime` with temporary application/data roots and synthetic trust
references, not real config or credentials. For all six F09 devices, pull and push
must preserve the builder's alias, address, user, independent HostKeyAlias and
strict known-host/identity options. Unknown/denied devices, wrong job metadata,
registry/config digest drift, alias drift, missing/incomplete helpers and malicious
paths/sessions must start zero processes. Pure argv assembly is checked under
file/resolve/process spies. Transfer execution uses only temporary logs, a real
temporary Store and a completed Popen substitute; no SSH or rsync is launched.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s services/headring-qc/tests -p test_governance_rsync_identity.py -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s services/headring-qc/tests -p 'test_*.py'
```

This closes the local argv bypass only. It is not evidence of actual data transfer,
production migration or device access; independent review and main integration
are still required.
