#!/usr/bin/env bash
# Build, publish, and verify the Android release declared in android/app/build.gradle.
#
# The last step is the point of the script: it downloads the APK back from the running
# server and compares checksums, so "published" means a phone can actually get this exact
# file — not merely that a copy landed in a directory.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
gradle_file="$root/android/app/build.gradle"
verify_url="${AUDIOREAP_ANDROID_VERIFY_URL:-http://localhost:8000/api/app/version}"

version_code="$(sed -nE 's/^[[:space:]]*versionCode[[:space:]]+([0-9]+)[[:space:]]*$/\1/p' "$gradle_file")"
version_name="$(sed -nE 's/^[[:space:]]*versionName[[:space:]]+"([^"]+)"[[:space:]]*$/\1/p' "$gradle_file")"
if [[ ! "$version_code" =~ ^[1-9][0-9]*$ || -z "$version_name" ]]; then
  echo "Could not read one Android versionCode and versionName from $gradle_file" >&2
  exit 1
fi

"$root/scripts/build-apk.sh"
apk="$root/android/app/build/outputs/apk/release/app-release.apk"
"$root/scripts/publish-android.sh" "$apk" "$version_code" "$version_name"

response="$(curl --fail --silent --show-error --header 'Cache-Control: no-cache' "$verify_url")"
read -r published_sha apk_url < <(
  python3 - "$response" "$version_code" "$version_name" <<'PY'
import json, sys
release = json.loads(sys.argv[1])
expected_code, expected_name = int(sys.argv[2]), sys.argv[3]
if release.get("versionCode") != expected_code or release.get("versionName") != expected_name:
    sys.exit(f'Live API reports {release.get("versionName")} ({release.get("versionCode")}), '
             f'expected {expected_name} ({expected_code})')
if not isinstance(release.get("sha256"), str) or not isinstance(release.get("apkUrl"), str):
    sys.exit("Live API response is missing sha256 or apkUrl")
print(release["sha256"], release["apkUrl"])
PY
)

download_url="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urljoin(sys.argv[2], sys.argv[1]))' "$apk_url" "$verify_url")"
downloaded_apk="$(mktemp --suffix=.apk)"
trap 'rm -f "$downloaded_apk"' EXIT
curl --fail --silent --show-error --location --header 'Cache-Control: no-cache' --output "$downloaded_apk" "$download_url"
downloaded_sha="$(sha256sum "$downloaded_apk" | cut -d ' ' -f 1)"
if [[ "$downloaded_sha" != "$published_sha" ]]; then
  echo "Downloaded APK checksum does not match the live release metadata" >&2
  exit 1
fi

echo "Verified live Android $version_name ($version_code) at $download_url"
