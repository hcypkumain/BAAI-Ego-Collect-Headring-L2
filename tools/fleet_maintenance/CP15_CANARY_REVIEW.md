# CP15 START / impedance canary — local candidate, not approved

This canary is limited to `cp_0015`; it does not discover devices and cannot deploy to another target. Runtime bytes are from Git commit `46b6538`, not the current worktree. The before gate is the complete 236-file stable manifest plus the five-file `36d805...` overlay, and the added diagnostic module must be absent. After state requires all 237 exact hashes. The static archive contains only the 15 reviewed candidate files, manifest and transaction program; it contains no runtime directory, token, configuration, calibration or recording.

Resource: `cp15-start-impedance-diagnostics-7fcb1f8dac51`.

| Resource | SHA-256 |
|---|---|
| manifest | `7fcb1f8dac518c173eee34f8da3069bb296a8ad25290e3256f7655ef0c31e731` |
| bundle | `a310b9ec5f5e060dbb02b7210726a54d50929c41dbace8f622f7f78ce5b34ede` |
| transaction | `7f04c444f54791dc4a49bb4bab026297a9794dbdb3cc4ab10bdd181ed3991941` |
| bootstrap | `c9b59dd6efaf1f0e21f2b269aa4bae4260c5fd92b09dc9f804735e1d6451bb66` |

Inspect `resources/<release>/manifest.json`, `transaction.py`, `bootstrap.py` and `cp15_release.py` before approval. Local-only resource validation:

```sh
python3 tools/fleet_maintenance/fleet.py --release cp15-start-impedance-diagnostics-7fcb1f8dac51 --targets cp_0015 --offline-plan --state-dir /tmp/opencode/cp15-canary
```

After independent code review and only with the user's device safely idle, a **read-only** plan may be requested (no implicit scanning):

```sh
python3 tools/fleet_maintenance/fleet.py --release cp15-start-impedance-diagnostics-7fcb1f8dac51 --targets cp_0015 --ssh-config /home/l2-headring-qc/headring-qc/ssh/config --address cp_0015=192.168.20.109 --bind-interface wlx40a5ef5ff812 --bind-address 192.168.20.100 --state-dir /tmp/opencode/cp15-canary
```

Only after review of that exact plan SHA and a new authoritative idle check may the main operator run the same command with `--apply --accepted-plan <PLAN_SHA256>` from the secure interactive PTY. The tool repeats the state/hash check and root-side transaction gates; no `--approve-unknown-baseline`. An all-after state skips mutation. An occupied device, prepared worker, active impedance or unknown ownership must not be interrupted.

The original independent review found a blocking orphan-ticket issue in `a3fe84a`; that version must not be deployed. The replacement `46b6538` and this transaction still require independent review before use. Tests here do not include hardware validation; `omnibci` worker tests could not import on the local host.

For later user-led acceptance: refresh the page to load the new ticket protocol; manually confirm the two wrist roles because the recorded role/calibration belongs to an older boot. Do not auto-calibrate or change camera exposure/resolution. Advisory high impedance remains WARN; missing, unpersisted or unrecovered evidence is still blocked. The operator must not initiate recording or impedance measurement as a deployment check.
