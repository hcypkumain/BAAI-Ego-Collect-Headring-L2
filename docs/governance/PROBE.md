# C: bounded read-only SSH adapter

Consumes C0 `Device`, `ProbePolicy`, `ProbeResult` unchanged. No registry file
reader, HTTP endpoint, daemon, thread, poller, cache store, device mutation or
production auto-enable is added. Main passes only `select_devices(...,
purpose='monitor')` results. Adapter revalidates the Device, enrollment, monitor
flag, digest syntax and generation; only main can establish registry membership
and that the selected objects share that digest. Empty scheduler creates nothing.

## API and admission (main must complete before live use)

`readonly_probe.probe_once(device, *, registry_digest, generation, policy)` does
one attempt. Its `timeout_ms` budget starts at public-call entry (including
validation, admission and cleanup), not at child spawn. Same-ID occupied/closing
ledger, exhausted global identity budget or process quarantine returns an immediate
null-valued `quarantined` result without spawning. No external watchdog is needed.
An admitted attempt returns `timeout` at the overall deadline, retaining earlier
accepted RAM if available. Cleanup uses only the remaining budget: if exhausted,
it immediately requests KILL, closes pipes, and retains unreaped handles in the
strong global ledger. Later once/tick activity reaps closed owners without retrying
their identity. There is no thread/background reaper or claim that an OS D-state
task can be forced to finish; callers must not interpret return as remote cleanup.
These are foreground/event-loop bounds, subject to Python/OS scheduling and the
same fundamental uninterruptible-kernel-I/O limitation, not hard realtime promises.

`isolated_transport.ProbeScheduler(devices, *, registry_digest,
policy, concurrency=2)` provides `tick(now_monotonic)`, `drain()`, `close(2.0)` and
integer-valued `stats()`. Call tick frequently (e.g. 5–20 ms); deadline checks and
fair service are event-loop driven, not an autonomous realtime guarantee.
Repeated tick without drain coalesces one result per device and will not launch
another attempt for that device until its pending result is drained.

There is **no live verification or deployment authorization in this delivery**.
Main/Astra must approve cancellation, account startup, explicit target, trust,
identity file and paths before invoking production SSH. No `ssh -G` is used.
Development opens only temporary synthetic config, never real trust/key/config.
The private `_test_scheduler(..., factory=...)` seam is local tests only; no
production command/plugin option exposes it.

### SSH configuration admission

Arbitrary SSH configuration is **not safe**, even with command-line overrides:
`Match exec` and `Include` can have evaluation side effects. The adapter reads
only the explicitly supplied `policy.ssh_config`, as a regular non-leaf-symlink
file, maximum 64 KiB. It never passes that file to SSH. Accepted grammar:

```text
Host synthetic-alias
  IdentityFile /synthetic/path/to/software-key
```

Blank lines and whole-line comments are allowed. Each Host is one exact token
matching `[A-Za-z0-9][A-Za-z0-9_.-]*`, no wildcard, duplicate section, Include,
Match, global directives, extra token, quotes, inline comments or other options.
Each section has at most one IdentityFile; selected section must have one.
IdentityFile is a canonical absolute path using only letters, digits, `_./-`,
without repeated separators, traversal, expansion or whitespace. The adapter does
not read keys. Parent directories/config file must be main-controlled (the local
parser does not prove ancestor ownership or prevent concurrent config replacement).
Main may create a separate local-only admission file; do not loosen this parser
to ingest an existing broad private config. No embedded real credential belongs
in Git, policy or diagnostic output.

Actual argv uses `/usr/bin/ssh -F /dev/null -T -i <admitted-path>`, explicit
HostName/address, HostKeyAlias and User from the **same** Device, BatchMode=yes,
StrictHostKeyChecking=yes, fixed explicit UserKnownHostsFile, global trust
`/dev/null`, KnownHostsCommand=none, UpdateHostKeys=no, VerifyHostKeyDNS=no.
Passwords/keyboard-interactive/agent/PKCS11 are disabled, publickey-only,
IdentitiesOnly=yes. Proxy/forwarding/local commands/control master/persistence
are disabled; no background mode or additional command. Known-host paths with
SSH `%`, `$`, `~` or quote expansion syntax are rejected.

Main must audit the referenced known_hosts file: pinned approved software host
keys for the exact HostKeyAlias, no wildcard/CA entries that widen trust, no
unapproved alternate identities; protect file and ancestors against replacement.
Main must approve a non-hardware, non-interactive software identity key and its
permissions. No security-key, PKCS11, agent, passphrase prompt or askpass workflow
is supported. This implementation does not claim every known_hosts/config is safe.

SSH remote exec necessarily traverses the remote account's login shell. Main
must audit its startup behavior and `/usr/bin/python3` availability/integrity;
`-I -S` isolates Python environment/site, not arbitrary shell startup effects.
SSH auditing may naturally write logs; this is not a claim of literally zero
filesystem activity on the device.

## Remote evidence and stage boundaries

Only the constant `/usr/bin/python3 -I -S -c <fixed-program>` executes remotely.
Strict locally serialized stdin JSON is at most 64 KiB. Policy paths are data,
never shell interpolations. The foreground program starts a 4.5-second SIGALRM;
disk stage resets a final 0.5-second alarm. It forks no process and never executes
source, shell snippets, services, HTTP, demand endpoints, scanners or hardware
tools. No service query is needed; controller/runtime remain unknown.

1. Pure identity file must be exactly device ID, optionally one trailing newline
   (32-byte cap), together with SSH strict host trust. Failure yields only null
   fields. No arbitrary JSON/config identity source is parsed.
2. Boot ID and MemAvailable from proc are flushed as a staged ProbeResult before
   component or disk reads. Timestamps are UTC sampling time, not freshness proof.
3. Explicit source files only: `.py`, `.sh`, `.service`, `.rules`, `.js`, `.ts`,
   `.toml`; no `/proc`, `/sys`, `/dev` sources. Each is regular, max 256 KiB, with
   no symlink in its traversed path, nonblocking open, bounded read. Main must
   audit these paths as non-secret ordinary sources and control their directories
   (checks do not eliminate rename/TOCTOU races). Output is SHA256 with
   `source=file:<path>`: **single-file observation**, not whole-component proof.
   Installed is never copied into runtime. Missing/read-rejected files stay unknown.
4. Disk defaults to None. Only explicit `statvfs` metadata in final bounded stage;
   no mount, discovery or directory scan. Alarm or local timeout preserves already
   accepted boot/RAM stages. Controller and runtime never derive from PID/service
   active status or source hashes; no fresh controller cache proof is implemented.

## Isolation, resource accounting and limitations

One direct SSH subprocess/process group is the entire local worker domain per
device; no wrapper process or extra thread. Scheduler is immutable in targets and
digest. A process-wide strong-reference ledger retains unreaped handles. A separate
identity-only quarantine dictionary survives local reap and new schedulers/digests.
The union of live IDs and quarantined IDs is capped at **64**, not 64 in each list;
no exited Popen is retained just to remember quarantine. Full budget rejects new
identities with an observable quarantined result and no spawn. Same-ID quarantine
cannot be bypassed by a new scheduler/once call; normal successful polls release
their live identity and can continue fairly. Dropping a scheduler reference cannot
erase an unreaped slot. All scheduler calls
must use the same main thread. Max 64 devices per scheduler, max 64 global
worker/SSH children, main process 1;
default **normal concurrency 2**. Round-robin service lets healthy devices advance
after the two hung domains exhaust 5 seconds + TERM 0.5 + KILL 0.5 seconds.
Unreapable domains release normal eligibility after that boundary but remain in
the ledger with handles; never replace a worker for that device. `poll` reaps
children; parent closes all pipes. `close` never spawns, waits at most caller's
timeout plus bounded tick processing, requests KILL early when the remaining budget
cannot accommodate the ordinary grace, returns false if any child is unreaped,
and can be retried to reap it. Caller must retain that scheduler/ledger on false.
Each pipe is readable/writable only after its own successful nonblocking setup.
Initialization/read/write/poll errors become device-local termination/quarantine;
unconfirmed pipes are closed without reading. Signal OSError retains the handle
and quarantine rather than aborting other devices' tick. A 30-ms close need not
spend 0.5 seconds waiting for TERM before requesting KILL.

**Conservative default: any timeout, invalid/missing output, rejected identity,
nonzero SSH exit or admission failure quarantines that device for this process
lifetime, even if reaped.**
Healthy devices continue sampling. There is no automatic timeout retry/reset or
namespace update API and **no production approved-reset entry point**. Tests alone
isolate/reset the private dictionary, only after synthetic child reap. Main must
not automatically recreate schedulers or CLI processes to retry quarantined IDs.
The identity record is in-process only, **not durable across a CLI/process restart**;
a fresh invocation is not proof of prior remote cleanup. Main must separately
review remote cleanup and future approved reset policy before any reattempt.

SIGKILL cannot guarantee local D-state reap or terminate a remote D-state reader.
Remote SIGALRM also cannot interrupt kernel uninterruptible sleep. This code does
not promise that killing local SSH kills every remote task; quarantine prevents
automatic accumulation from repeated attempts. Synthetic unreapable tests prove
ledger/scheduling behavior, **not actual D-state cancellation**. Main's production
acceptance must preserve this limitation, not turn fixtures into a cancellation
claim. No actual device connection was made by C.

IPC uses strict newline-delimited JSON, no pickle. Total stdout per domain is
bounded by policy (maximum 1 MiB), stderr is drained/discarded with an 8192-byte
saturating counter (no retained/echoed secret). Per tick read budget is 128 KiB
per stream/domain plus one bounded exit-drain pass. Overflow or malformed,
duplicate-key, NaN, wrong device/digest/generation results are rejected and the
domain stopped. After deadline no new stage is accepted. Each domain retains one
latest stage, one incomplete line, and at most 64 KiB pending stdin; each device
one coalesced result. Serialized retained payload budget is bounded by roughly
64 × (3 MiB + 64 KiB), plus bounded read chunks/Python representation overhead.
`stats.bytes` reports serialized retained buffers/stages/results, **not total RSS
or allocator overhead**. `workers/children` include unreapable ledger entries,
`active` includes TERM/KILL grace until quarantine, `quarantined` counts denied
devices even after their child is reaped, `queued` counts pending device results;
spawned/reaped/rejected are lifetime counters; global_workers/global_children
count the shared process-wide handle ledger; global_quarantined counts identity
tombstones and global_identities counts their union with live IDs (maximum 64).
The at-most-64 identity records contain no Popen/pipe/payload. Read FD count is at most 3 per
running domain, with no persistent result storage.

## Local acceptance

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s host/qc/tests -p 'test_governance_*.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s host/common/tests -p 'test_*.py'
git diff --check
```

Tests execute real local subprocesses (including two ignoring SIGTERM), constant
remote reader against temporary identity/source fixtures and local proc, synthetic
disk hang, wrong identity/envelope, overflow, zero targets, per-device generation,
bounded close and synthetic unreapable poll. One run uses the real default
5000-ms policy and requires three healthy rounds within 8 seconds. A separate
100 distinct short-timeout subprocess test measures FD/RSS/tracemalloc, explicitly
resetting test-only identity state after each confirmed reap; a further 100
post-timeout ticks confirms quarantined devices never receive replacements.
G1 regressions add direct 50-ms once calls against same-ID/full/closing ledgers
(no watchdog), running-once total-budget checks, new-digest quarantine after reap,
combined live+tombstone capacity, actual ignore-TERM children with injected pipe
setup failure and 30-ms close, healthy third progress and device-local I/O errors.
All process factories are local Python. No SSH subprocess, production config,
trust/key file, network, DB or live QC service is used by tests.
