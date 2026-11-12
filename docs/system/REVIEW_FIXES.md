# Phase3 review correction R1–R3

The independent review fixed its subject at `ae60c5c`. It did **not** sign off
phase3 completion: R1/R2 were high and R3 medium. This follow-up addresses only
those findings. Independent re-review is pending; phase4A has not started.

## Corrected contracts

| Finding | Reproduction | Minimal correction |
| --- | --- | --- |
| R1 | Device BOOTTIME=100000; same-boot RAM sampled=0, age=0, quality=known, TTL=15000 | Reject underestimated age before host acceptance, even with verified identity. For same-boot timestamped evidence, `age >= device_publish_mono - sampled_mono`. Known also retains the existing TTL/future checks. |
| R2 calibration | Unknown snapshot, confirmed/calibration_valid=true but null boot/binding/version/epoch/proof | A true assertion requires non-null current boot, binding, calibration version, proof boot and matching non-null profile epoch, plus confirmed=true. |
| R2 review | Measurement/owner/version and matching session, but both profile epochs null | A true assertion requires non-null current boot/controller, session/profile epoch and measurement/owner/quality version; session and epoch must match current context. Null never supplies identity proof. |
| R3 | Sixteen idle SSE peers RST; subscriptions remain occupied; seventeenth disconnects without status; stalled writer retains slot | Idle SSE writes a comment heartbeat every 0.5s; socket writes have a 1s timeout; every admitted subscription leaves through finally/unsubscribe. Capacity failure returns typed 503 CAPACITY_EXCEEDED. |

Age is deliberately a **lower bound**, not an equality: host response age and
local elapsed can increase age while device publish time remains frozen.
Host-restored stale evidence with null age stays legal. No UTC comparison or
timestamp reset is used to make old evidence fresh. No measurement TTL, WARN
rule, ledger or control gate was changed. Completed review authority remains
true after `aged(...,60000)` when its full identity proof is valid; a controller
reset still clears current publisher measurement/role data. Unknown remains null.

The documentation-only validator mirrors these semantic checks; original schemas
and 25 fixture expectations remain unchanged. `shadow-diff.json` adds explicitly
synthetic AGE_UNDERESTIMATED and NULL_NOT_PROOF cases, not new device evidence.

## Red → green evidence

Before editing implementation, two new regression files were run on `ae60c5c`
with only the tests added:

```sh
python3 -m unittest discover -s orangepi/tests -p 'test_snapshot_review.py' -v
python3 -m unittest discover -s host/qc/tests -p 'test_review_regressions.py' -v
```

Device: 5 tests, 2 pass / 3 fail (age and null proofs were accepted).
Host/SSE: 5 tests, 4 fail / 1 error (including the actual seventeenth HTTP
connection's RemoteDisconnected). After the fix all 10 pass. SSE tests use real
ephemeral loopback sockets, actual SO_LINGER RST, a tiny receive/send window and
an open non-reading peer; no production config, database, service or device.
The heartbeat does not change revision/observation tickets or invoke touch,
History.sync or Hub.start. Existing 16-client/8-event limits and overflow reset
remain unchanged.

The original 13 tests remain: updated full suites contain device 12 + host 11
tests (23 total). They still exercise original 25 examples, actual authorized
device HTTP/304, response age/host elapsed/restart stale, immutable publisher,
full HTTP→Hub→both real UI consumers/renderer, shadow and GET purity. Local
before/after logs and exact rerun results are referenced in the delivery report.

Scope ends here. The separately read 4A plan is a conditional future handoff,
not authorization to implement a sampler, poller, persistence or deployment.
