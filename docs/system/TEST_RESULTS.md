# Offline phase3 verification

Environment: Python 3.14.4, jsonschema 4.19.2, referencing 0.36.2, Node
v26.13.0, all pre-existing. No dependencies installed. No hardware acceptance.

R1–R3 follow-up: `REVIEW_FIXES.md` records the independent review findings and
actual red→green reproductions. The 10 new regressions pass; the 5-test host
review subset (including real RST/capacity/slow-reader sockets) also passed three
additional repetitions. All legacy comparison commands below were rerun in both
the unchanged baseline and corrected branch, with the same outcomes. Independent
re-review/signoff remains pending; phase4A was not implemented.

Run from repository root:

| Exact command | Result |
| --- | --- |
| `python3 contracts/v1/validate_examples.py` | 9 JSON / 4 schemas / original 25 case expectations PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_snapshot*.py' -v` | 12 PASS after R1/R2 corrections (original 7 + 5 review regressions) |
| `python3 -m unittest discover -s host/qc/tests -p 'test_*.py' -v` | 11 PASS after review corrections (original 6 + 5 review regressions); includes device HTTP → Hub → both consumer HTTP → actual JS consumer/renderer chain |
| `python3 tools/system_read_model_fixture.py --output /tmp/opencode/system-v1-fixture.json` | synthetic five-card host schema validated |
| `node host/qc/tests/test_fleet_cards.cjs /tmp/opencode/system-v1-fixture.json` | PASS shared actual renderer, actual receiver entry, actual workbench consumer; five distinct quality/reboot examples |
| `node --check tools/fleet_maintenance/static/app.js` | PASS |
| `node --check host/qc/fleet-v1.js` | PASS |

The device route test extracts the real nested Dashboard Handler with AST,
injecting only its dependencies; it does not instantiate a hardware controller
or token-writing Dashboard. It tests 100 reads with alternating 200/304 and
100 each auth-failure/query-invalid requests under IO spies. Host tests perform
100 paired pure full reads from actual Hub and actual maintenance handlers plus
100 query-invalid reads. Real local HTTP SSE returns a reset event without
starting telemetry; bounded queue overflow and full restoration are tested.

## Same-environment baseline comparison

Baseline `01bd5013112d31686c738e9aeadc55e3153c6d81` was extracted with
`git archive` (only orangepi and tools/fleet_maintenance) to an approved temp
directory, without switching/resetting any existing worktree. The commands below
were run unchanged in baseline and current source directories:

| Command | Baseline | Current |
| --- | --- | --- |
| `python3 -m unittest discover -s tools/fleet_maintenance/tests -p 'test_*.py'` | 107 PASS | 107 PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_dashboard.py'` | 26 PASS | 26 PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_impedance_advisory.py'` | 14 PASS | 14 PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_stop_identity.py'` | 9 PASS | 9 PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_stop_state_publish.py'` | 3 PASS | 3 PASS |
| `python3 -m unittest discover -s orangepi/tests -p 'test_mode_recovery.py'` | 6 PASS, 2 ERROR | 6 PASS, 2 ERROR |

The identical existing mode-recovery errors are
`test_background_refresh_does_not_drop_accepted_mode_job` and
`test_queue_still_rejects_recording_revision_and_profile_changes`: the fixture
Dashboard lacks `privacy`, then its extracted method lacks `ScanProcessError`.
These are **not counted as passing** and were not changed in this slice.
Existing Dashboard HTTPError ResourceWarnings and fleet's negative duplicate-ZIP
member warning appear in both runs. Full hardware-dependent suites were not run.

## Archive / safety checks

- Every original phase2 source hash reverified against ARCHIVE_MANIFEST; every
  original Q allowlisted source hash reverified against SOURCE_MANIFEST. Originals
  remain unchanged.
- Staged source/docs inspected for private host absolute paths, private-key
  headers, literal bearer/API credentials and GitHub tokens, plus per-file size
  bounds. No findings; no ZIP artifacts present. This heuristic plus source
  review is not a mathematical proof of absence of every possible secret.
- `git diff --cached --check` passes. No production HTTP ports, device connections,
  database constructors, raw scans, deployment or live samplers were exercised.
- During initial test integration the archived recovery module transitively
  imported its private recovery helper. This was identified and removed from the
  v1 import path by lazy legacy import plus private-workspace redaction. Final
  v1 tests do not import that adapter or construct its stores; no live operation
  was invoked by those initial tests.
