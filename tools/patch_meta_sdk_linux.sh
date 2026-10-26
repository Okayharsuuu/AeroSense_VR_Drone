#!/usr/bin/env bash
# Fixes a Meta XR SDK 201 bug that stops the project building in the Linux editor.
#
# On Linux the OVRPlugin native library does not load in the editor, so the SDK
# reads its version as 60 and OVRProjectConfig.cs builds a list with a negative
# length (ArgumentOutOfRangeException every 5 seconds, then a failed build).
#
# This copies the SDK out of Library/PackageCache into Packages/ as an embedded
# package (Unity prefers it over the downloaded one) and clamps that length to 0.
# The embedded copy is gitignored, so each Linux machine runs this once.
# Windows machines do not need it.
#
# Usage: close Unity, open the project once first so the SDK is downloaded, then
#   bash tools/patch_meta_sdk_linux.sh
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="Library/PackageCache/com.meta.xr.sdk.core@201.0.0"
DST="Packages/com.meta.xr.sdk.core"
FILE="$DST/Editor/OVRProjectConfig.cs"

if [ -f "$FILE" ] && grep -q "Math.Max(0, currentSdkVersion" "$FILE"; then
    echo "Already patched: $FILE"
    exit 0
fi

if [ ! -d "$SRC" ]; then
    echo "Not found: $SRC"
    echo "Open the project in Unity 2022.3.62f2 once so it downloads the SDK, close Unity, then run this again."
    exit 1
fi

rm -rf "$DST"
cp -r "$SRC" "$DST"
chmod -R u+w "$DST"

sed -i 's/Enumerable.Range(version2Start, currentSdkVersion - version2Start + 1)/Enumerable.Range(version2Start, Math.Max(0, currentSdkVersion - version2Start + 1))/' "$FILE"

if grep -q "Math.Max(0, currentSdkVersion" "$FILE"; then
    echo "Patched $FILE"
    echo "Reopen the project in Unity."
else
    echo "The patch did not apply. The SDK file may have changed; check line ~117 of $FILE."
    exit 1
fi
