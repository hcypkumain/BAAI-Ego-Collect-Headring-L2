#!/usr/bin/env bash
# Build and sign with locally supplied Android SDK and an existing private key.
set -euo pipefail
: "${JAVA_HOME:?Set JAVA_HOME to a JDK 17 directory}"
: "${ANDROID_HOME:?Set ANDROID_HOME to an Android SDK directory}"
: "${ANDROID_KEYSTORE:?Set ANDROID_KEYSTORE to an existing signing keystore}"
: "${ANDROID_KEYSTORE_PASS:?Set ANDROID_KEYSTORE_PASS in the environment}"
: "${ANDROID_KEY_ALIAS:?Set ANDROID_KEY_ALIAS to the signing key alias}"
version_code="${HEADRING_VERSION_CODE:-3}"
if [[ ! "$version_code" =~ ^[1-9][0-9]{0,8}$ ]]; then printf 'Invalid HEADRING_VERSION_CODE\n' >&2; exit 2; fi
if [[ $# != 1 ]]; then printf 'Usage: %s OUTPUT_APK\n' "$0" >&2; exit 2; fi
output="$1"
if [[ "$output" != /* ]]; then output="$PWD/$output"; fi
app="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$ANDROID_HOME"
java="$JAVA_HOME"
tools="$sdk/build-tools/35.0.0"
platform="$sdk/platforms/android-35/android.jar"
[[ -f "$platform" && -x "$tools/aapt2" && -x "$tools/d8" && -x "$tools/apksigner" && -x "$tools/zipalign" ]]
tmp="$(mktemp -d "${TMPDIR:-/tmp}/headring-android-build.XXXXXXXX")"
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/classes" "$tmp/dex"
export JAVA_HOME="$java" PATH="$java/bin:$PATH"
mapfile -d '' sources < <(find "$app/src" -name '*.java' -print0 | sort -z)
[[ ${#sources[@]} -gt 0 ]]
"$java/bin/javac" --release 8 -classpath "$platform" -d "$tmp/classes" "${sources[@]}"
"$tools/aapt2" compile --dir "$app/res" -o "$tmp/resources.zip"
cp "$app/AndroidManifest.xml" "$tmp/AndroidManifest.xml"
"$tools/aapt2" link -I "$platform" --manifest "$tmp/AndroidManifest.xml" -R "$tmp/resources.zip" --min-sdk-version 26 --target-sdk-version 35 --version-code "$version_code" --version-name "0.$version_code" -o "$tmp/base.apk"
mapfile -d '' classes < <(find "$tmp/classes" -name '*.class' -print0 | sort -z)
"$tools/d8" --min-api 26 --lib "$platform" --output "$tmp/dex" "${classes[@]}"
(cd "$tmp/dex" && zip -q "$tmp/base.apk" classes.dex)
"$tools/zipalign" -f 4 "$tmp/base.apk" "$tmp/aligned.apk"
"$tools/apksigner" sign --ks "$ANDROID_KEYSTORE" --ks-key-alias "$ANDROID_KEY_ALIAS" --ks-pass env:ANDROID_KEYSTORE_PASS --out "$output" "$tmp/aligned.apk"
"$tools/apksigner" verify --verbose "$output"
sha256sum "$output"
