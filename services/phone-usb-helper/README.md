# Offline phone helper (CP21 field candidate)

This directory records the Android helper and the device-side services used for the CP21 offline USB entry. The helper presents the existing collection UI inside a WebView, through an HTTP stream proxy bound to phone loopback. Device discovery is split from the `Endpoint` model and proxy so future Wi-Fi providers can be added; this implementation enables USB discovery only. The existing collection page and its security checks remain on the device.

The Android app requires Android 8.0 or newer and USB tethering; USB debugging, root and internet are not required for operation. It discovers `_headring._tcp` by NSD and, where available, a bounded scan of the selected USB subnet. It verifies a read-only device identity response before selecting the device; this is an unauthenticated self-declaration, not cryptographic proof. The user can enter a current IP once to diagnose the connection and learn an eligible USB interface. Only the interface name, not that IP, is saved. The fixed in-app origin is `http://localhost:8765`.

`device/` contains the deployed CP21-specific dynamic Avahi supervisor, read-only discovery/download service, DHCP recovery guard and systemd unit files. Their paths, device ID, USB selector, service names and proof payload location are deliberately specific to this field setup. The Avahi guard depends on the separately prepared, root-owned binary, library, metadata and configuration under `/opt/headring-usb-entry-proof/proof-20261113-b524dab0`; it must not be deleted while these services run. The DHCP guard applies an in-memory NetworkManager repair only to the selected USB phone when it has inherited the old static `usb0` gadget profile. It does not rewrite the gadget profile or modify the WLAN route. These files are a field record, not a generic installer; adapting them to another device requires reviewing identity, USB and artifact paths.

## Android build

Use JDK 17, Android SDK platform 35 and build-tools 35.0.0, plus `zip`. Supply an existing signing keystore outside the repository. To upgrade an installed copy, the signing key must match that copy. Never commit the keystore or its password.

```sh
export JAVA_HOME=/path/to/jdk17
export ANDROID_HOME=/path/to/android-sdk
export ANDROID_KEYSTORE=/secure/path/signing.p12
export ANDROID_KEY_ALIAS=phone-helper
read -rs ANDROID_KEYSTORE_PASS; export ANDROID_KEYSTORE_PASS
./services/phone-usb-helper/android/build.sh /absolute/path/headring-helper.apk
```

The build script creates temporary intermediate files and signs using the password from the environment. Private signing material, SDKs and built APKs are intentionally excluded from this repository.

## Verification and limits

The v3 APK was compiled and signature-verified; the blob download hook was checked with a 100,003-byte multi-chunk test. The device identity probe, APK download and DNS-SD publisher were checked on CP21. A USB interface rename and an inherited static address were corrected, and the user reported the app working in the field. Full coverage of camera streams, upload and download variants, all Android versions, reboot and repeated unplug/reconnect remains unverified. The UI's system file picker and save dialog are required for file transfers; in-app downloads are limited to 32 MB. Background operation depends on Android and vendor power-management behavior.

Run focused host tests with:

```sh
python3 -m unittest discover -s services/phone-usb-helper/device/tests -v
python3 -m unittest discover -s services/phone-usb-entry-avahi/tests -v
```
