#!/usr/bin/env bash
# Build and sign with locally supplied Android SDK and an existing private key.
set -euo pipefail
: "${JAVA_HOME:?Set JAVA_HOME to a JDK 17 or newer directory}"
: "${ANDROID_HOME:?Set ANDROID_HOME to an Android SDK directory}"
unsigned=false
parallel=false
while [[ "${1:-}" == --* ]]; do
 case "$1" in --unsigned) unsigned=true;; --parallel) parallel=true;; *) printf 'Unknown option: %s\n' "$1" >&2; exit 2;; esac
 shift
done
if ! "$unsigned"; then
 : "${ANDROID_KEYSTORE:?Set ANDROID_KEYSTORE to the existing release keystore, or use --unsigned}"
 : "${ANDROID_KEYSTORE_PASS:?Set ANDROID_KEYSTORE_PASS in the environment}"
 : "${ANDROID_KEY_ALIAS:?Set ANDROID_KEY_ALIAS to the existing release alias}"
fi
version_code="${HEADRING_VERSION_CODE:-9}"
if [[ ! "$version_code" =~ ^[1-9][0-9]{0,8}$ ]]; then printf 'Invalid HEADRING_VERSION_CODE\n' >&2; exit 2; fi
if [[ $# != 1 ]]; then printf 'Usage: %s [--unsigned] [--parallel] OUTPUT_APK\n' "$0" >&2; exit 2; fi
output="$1"
if [[ "$output" != /* ]]; then output="$PWD/$output"; fi
app="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$ANDROID_HOME"
java="$JAVA_HOME"
tools="$sdk/build-tools/35.0.0"
platform="$sdk/platforms/android-35/android.jar"
[[ -f "$platform" && -f "$tools/core-lambda-stubs.jar" && -x "$tools/aapt2" && -x "$tools/d8" && -x "$tools/apksigner" && -x "$tools/zipalign" ]]
[[ ! -e "$output" ]] || { printf 'Refusing to overwrite APK: %s\n' "$output" >&2; exit 2; }
tmp="$(mktemp -d "${TMPDIR:-/tmp}/headring-android-build.XXXXXXXX")"
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/classes" "$tmp/dex"
export JAVA_HOME="$java" PATH="$java/bin:$PATH"
mapfile -d '' sources < <(find "$app/src" -name '*.java' -print0 | sort -z)
[[ ${#sources[@]} -gt 0 ]]
if "$parallel"; then
 grep -q 'static final int PORT=8765;' "$app/src/org/headring/usbentry/EntryAddress.java"
 sed 's/static final int PORT=8765;/static final int PORT=18765;/' "$app/src/org/headring/usbentry/EntryAddress.java" > "$tmp/EntryAddress.java"
 mapfile -d '' sources < <(find "$app/src" -name '*.java' ! -name EntryAddress.java -print0 | sort -z)
 sources+=("$tmp/EntryAddress.java")
fi
# Android's boot classes, not the host JDK APIs, define the supported surface.
compiler=("$java/bin/java" com.sun.tools.javac.Main)
if [[ -x "$java/bin/javac" ]]; then compiler=("$java/bin/javac"); fi
"${compiler[@]}" -source 8 -target 8 -bootclasspath "$platform:$tools/core-lambda-stubs.jar" -d "$tmp/classes" "${sources[@]}"
"$tools/aapt2" compile --dir "$app/res" -o "$tmp/resources.zip"
cp "$app/AndroidManifest.xml" "$tmp/AndroidManifest.xml"
link_args=()
if "$parallel"; then
 sed -i \
  -e 's/android:name=".MainActivity"/android:name="org.headring.usbentry.MainActivity"/' \
  -e 's/android:name=".EntryService"/android:name="org.headring.usbentry.EntryService"/' \
  -e 's/android:name=".BootReceiver"/android:name="org.headring.usbentry.BootReceiver"/' "$tmp/AndroidManifest.xml"
 link_args+=(--rename-manifest-package org.headring.usbentry.v5)
fi
"$tools/aapt2" link -I "$platform" --manifest "$tmp/AndroidManifest.xml" "$tmp/resources.zip" -A "$app/assets" --min-sdk-version 26 --target-sdk-version 35 --version-code "$version_code" --version-name "0.$version_code" "${link_args[@]}" -o "$tmp/base.apk"
mapfile -d '' classes < <(find "$tmp/classes" -name '*.class' -print0 | sort -z)
"$tools/d8" --min-api 26 --lib "$platform" --output "$tmp/dex" "${classes[@]}"
(cd "$tmp/dex" && zip -q "$tmp/base.apk" classes.dex)
"$tools/zipalign" -f 4 "$tmp/base.apk" "$tmp/aligned.apk"
if "$unsigned"; then
 cp "$tmp/aligned.apk" "$output"
 printf 'UNSIGNED verification artifact: release signing is required before installation.\n'
else
 "$tools/apksigner" sign --ks "$ANDROID_KEYSTORE" --ks-key-alias "$ANDROID_KEY_ALIAS" --ks-pass env:ANDROID_KEYSTORE_PASS --out "$output" "$tmp/aligned.apk"
 "$tools/apksigner" verify --verbose "$output"
fi
sha256sum "$output"
if "$parallel"; then printf 'Parallel package: org.headring.usbentry.v5; local entry port18765; existing App is retained.\n'; fi
