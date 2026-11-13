# Offline collection and phone helper

The supported target is an **already initialized arm64 collector**, with its existing hardware runtime, Python virtual environment, systemd collector service, camera daemon, device identity and calibration in place. This is not a bare-OS provisioning image. The offline release contains the complete 121-file collector source baseline `f5d03b7b5fb197eb76e78610a0bce672e5cc4853`, the Android app, the USB discovery services and the pinned Ubuntu Avahi daemon/core library and licences. It does not contain calibration, registry, credentials, datasets, Python virtualenv, camera binaries or signing keys.

The default `apply` handles the entire source baseline and phone service together. If collector source already matches, it does not stop or restart the collector. If source differs, the transaction requires the active collector and camera to report idle, obtains the existing capture lease, backs up the changed files and restarts only the collector after replacing them. Unknown, busy or missing runtime is a refusal, not a partial installation. The former USB debugging helper is optional compatibility and is not installed, started or required by this release.

## Build a self-contained offline bundle

On a build computer with Git, Python 3.9+, `dpkg-deb`, JDK 17 and Android SDK build-tools/platform 35, obtain these two exact Ubuntu packages into `DEPS`; the builder verifies SHA-256 and extracts only the daemon, core library and copyright files, without executing package scripts:

```sh
DEPS=./avahi-debs
mkdir -p "$DEPS"
curl -fL -o "$DEPS/avahi-daemon_0.7-4ubuntu7.3_arm64.deb" https://ports.ubuntu.com/ubuntu-ports/pool/main/a/avahi/avahi-daemon_0.7-4ubuntu7.3_arm64.deb
curl -fL -o "$DEPS/libavahi-core7_0.7-4ubuntu7.3_arm64.deb" https://ports.ubuntu.com/ubuntu-ports/pool/main/a/avahi/libavahi-core7_0.7-4ubuntu7.3_arm64.deb
export JAVA_HOME=/path/to/jdk17 ANDROID_HOME=/path/to/android-sdk
export PATH="$JAVA_HOME/bin:$PATH"
python3 deployment/phone-usb-entry/build_bundle.py \
  --device cp_0021 --usb-port 7-1.2.2.2 --vid 2717 --pid ff80 \
  --deps "$DEPS" --apk services/phone-usb-helper/android/artifacts/headring-helper-cp0021-v3.apk \
  --android-sdk "$ANDROID_HOME" --output headring-cp0021.tar
```

The builder prints the bundle SHA-256 and release ID. Record that SHA from a trusted channel. The included CP21 APK is the user-validated v3 artifact, SHA-256 `6176b00e6004e4e388c68916f3a0aed0137b912d2b7dfa131de744faca81181a`, certificate SHA-256 `a382787931ef1937c8a318a080b912fdbb5f06ce7de0265d08bc398a12437bfe`. Its signing key is not in this repository. Future updates to an installed APK must use the same external signing key.

For a different target, build an APK with the same target identity used in `--device`. Set `HEADRING_DEVICE_ID=cp_0033` (example), `JAVA_HOME`, `ANDROID_HOME`, `ANDROID_KEYSTORE`, `ANDROID_KEY_ALIAS` and `ANDROID_KEYSTORE_PASS`, then run `services/phone-usb-helper/android/build.sh /absolute/path/helper.apk`. Supply the resulting APK and the target's verified USB port, VID and PID to the bundle builder. The builder validates the APK identity and signer; it does not create or store a signing key. Target-selector values are explicit; CP21 is not silently assumed on other devices.

## Plan, apply and roll back

Transfer the bundle to the target and independently verify its trusted hash before extracting the embedded deployment program. Example, substituting the actual output hash:

```sh
BUNDLE=/path/to/headring-cp0021.tar
SHA256=the_64_hex_digits_printed_by_the_builder
printf '%s  %s\n' "$SHA256" "$BUNDLE" | sha256sum -c -
tar -xOf "$BUNDLE" support/deploy_bundle.py > /tmp/headring-offline-deploy.py
sudo python3 /tmp/headring-offline-deploy.py plan --bundle "$BUNDLE" --sha256 "$SHA256"
sudo python3 /tmp/headring-offline-deploy.py apply --bundle "$BUNDLE" --sha256 "$SHA256"
```

The last command is the one-command deployment after the one-time bundle verification. The target must be Linux arm64 with Python 3.8+, systemd, NetworkManager and the existing collector runtime; the plan checks device identity, active collector path, the camera-status socket, arm64 ELF and Avahi system library resolution, bundle hashes, port/runtime conflicts, and all 121 source files. It accepts the known prior CP21 phone services only when their unit hashes match. It does not run apt, change routes or disable the existing ADB service. No default collector code changes or restarts occur when source matches.

Each transaction is journalled under `/var/lib/headring-offline/transaction-<release-id>/` with backups and pre/post hashes. To undo a completed release, while the collector is confirmed idle:

```sh
sudo python3 /tmp/headring-offline-deploy.py rollback \
  --transaction /var/lib/headring-offline/transaction-offline-RELEASEID
```

Use the exact release ID printed by the builder. Rollback refuses files altered after deployment, checks backups first and uses the same capture lease for collector or legacy-phone changes. A newly created non-login `avahi` account is retained on rollback and recorded in the journal; it is never removed automatically because other services could subsequently use it. A failed transaction with `rollback_needs_review` must be inspected before retrying.

## Acceptance and limits

The app presents the existing page in an embedded WebView and uses `http://localhost:8765` internally. Discovery uses USB DNS-SD and a bounded USB subnet scan with a read-only identity probe; that self-declared identity is not cryptographic authentication. `DiscoveryProvider` and `Endpoint` are separated for future wireless work, but wireless discovery is not implemented. The user confirmed the CP21 app worked; repeated unplug/replug, reboot, all Android variants and all upload/download paths are not established by that confirmation. The system picker and save dialog handle files; in-app downloads are limited to 32 MB. Vendor battery policies may interrupt background discovery.

The deployment transaction and failure paths are tested using isolated fixtures and mocked system services; no production collector is restarted by those tests. Deployment of this new release itself has not yet been executed or accepted on a physical target.
