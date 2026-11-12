# Work-in-progress backup, not a release

This branch preserves unfinished LAN time/acquisition locking changes and the fleet workbench (Phase 1, 2A and USB 2B). It is a recovery snapshot, not an approved deployment or a fully regression-tested release. The original release branch and its fixed build pin remain unchanged. Review the time-lock migration and its tests before any production use; do not install over live acquisition processes.

The separately registered EEG impedance advisory overlay is archived under `resources/eeg-impedance-advisory-36d8059918ce/`, including its five-file bundle, manifest, transaction, bootstrap, source-and-test patch and tests. It has **not** been merged into the five main source files. The release note records the limited device verification and the absence of physical impedance/recording validation. The bundle is a fixed reviewed artifact, not permission to deploy.

Private runtime state, credentials, recordings, device inventories, original images, logs and protected snapshots are not part of this backup. Some scripts necessarily retain operational device-path defaults; revalidate all configuration and safety gates before use.

On a fresh clone, the builder additionally requires the local release branch to point at the original pin. If that local branch does not already exist, create its tracking reference without changing an existing branch:

```sh
git fetch origin release/headring-v8.5-integrity-stop
git branch --track release/headring-v8.5-integrity-stop origin/release/headring-v8.5-integrity-stop
```

Verify that this local branch resolves to `f5d03b7b5fb197eb76e78610a0bce672e5cc4853`; if it exists at another commit, investigate rather than overwrite it.
