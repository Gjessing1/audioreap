#!/usr/bin/env bash
# Publish a signed audioreap APK for direct download and in-app update checks.
# Usage: ./scripts/publish-android.sh <apk> <versionCode> <versionName>
#
# Writes the APK and a version.json describing it into the directory the api container
# serves /api/app/{version,download} from. version.json is written to a temporary file
# and renamed, so a phone polling mid-publish reads either the old release or the new
# one — never a half-written one pointing at a file that is still being copied.
set -euo pipefail

apk="${1:?path to the signed APK}"
version_code="${2:?Android versionCode (integer)}"
version_name="${3:?version name, for example 0.1.0}"

# The api container's AUDIOREAP_ANDROID_APP_DIR (/data/app) as seen from the host.
# Publishing anywhere else succeeds silently and the phone never sees the update.
if [[ -n "${AUDIOREAP_ANDROID_APP_DIR:-}" ]]; then
  app_dir="$AUDIOREAP_ANDROID_APP_DIR"
elif [[ -d /mnt/data/audioreap ]]; then
  app_dir="/mnt/data/audioreap/app"
else
  echo "No audioreap data directory found; set AUDIOREAP_ANDROID_APP_DIR explicitly" >&2
  exit 1
fi

if [[ -f "$HOME/android-sdk/env.sh" && -z "${ANDROID_HOME:-}" ]]; then
  # shellcheck source=/dev/null
  . "$HOME/android-sdk/env.sh"
fi

if [[ ! -f "$apk" ]]; then
  echo "APK not found: $apk" >&2
  exit 1
fi
if [[ ! "$version_code" =~ ^[1-9][0-9]*$ ]]; then
  echo "versionCode must be a positive integer" >&2
  exit 1
fi
if [[ ! "$version_name" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]]; then
  echo "versionName may contain only letters, numbers, dots, underscores, and dashes" >&2
  exit 1
fi

# The APK is offered to every phone that asks, so what goes out is checked first: it must
# be the build these version numbers claim, and it must be signed — a debuggable or
# unsigned build reaching a device is a worse outcome than a failed publish.
if [[ -z "${ANDROID_HOME:-}" ]]; then
  echo "ANDROID_HOME is required to verify an APK before publication" >&2
  exit 1
fi
aapt="$(find "$ANDROID_HOME/build-tools" -mindepth 2 -maxdepth 2 -type f -name aapt | sort -V | tail -1)"
apksigner="$(find "$ANDROID_HOME/build-tools" -mindepth 2 -maxdepth 2 -type f -name apksigner | sort -V | tail -1)"
if [[ -z "$aapt" || -z "$apksigner" ]]; then
  echo "Android build tools (aapt and apksigner) are required to publish an APK" >&2
  exit 1
fi

badging="$("$aapt" dump badging "$apk")"
if [[ "$badging" == *"application-debuggable"* ]]; then
  echo "Refusing to publish a debuggable APK: $apk" >&2
  exit 1
fi
if [[ "$badging" != *"versionCode='$version_code'"* ]]; then
  echo "APK versionCode does not match $version_code" >&2
  exit 1
fi
if [[ "$badging" != *"versionName='$version_name'"* ]]; then
  echo "APK versionName does not match $version_name" >&2
  exit 1
fi
if ! "$apksigner" verify "$apk"; then
  echo "Refusing to publish an unsigned or invalid APK: $apk" >&2
  exit 1
fi

file="audioreap-$version_name.apk"
mkdir -p "$app_dir"
install -m 0644 "$apk" "$app_dir/$file"
bytes="$(stat -c '%s' "$app_dir/$file")"
sha256="$(sha256sum "$app_dir/$file" | cut -d ' ' -f 1)"
metadata_tmp="$app_dir/.version.json.$$"
printf '{"versionCode":%s,"versionName":"%s","file":"%s","bytes":%s,"sha256":"%s"}\n' \
  "$version_code" "$version_name" "$file" "$bytes" "$sha256" > "$metadata_tmp"
chmod 0644 "$metadata_tmp"
mv "$metadata_tmp" "$app_dir/version.json"

echo "Published $file (versionCode $version_code) to $app_dir"
