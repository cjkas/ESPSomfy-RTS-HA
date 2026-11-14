#!/usr/bin/env bash
# Writes a release's version into the integration: bash .github/set_version.sh v3.0.4
# The release job zips the integration with it, and commits it with
# --if-newer, which leaves a newer version alone (a rerun of an older
# release's job).
# Tested by tests/scripts/test_set_version.sh.
set -euo pipefail

IF_NEWER=
if [[ "${1-}" == --if-newer ]]; then
  IF_NEWER=1
  shift
fi

VERSION="${1-}"
VERSION="${VERSION#v}"
# Home Assistant blocks a custom integration whose manifest version it can't
# parse, and the version goes into sed replacements, so only X.Y.Z with a
# SemVer or PEP 440 pre-release: 3.0.4, 3.1.0-beta.1, 4.0.0-rc1, 3.1.0b1.
# Not 3.1.0.beta, which Home Assistant refuses.
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*|(a|b|rc)[0-9]+)?$ ]]; then
  echo "Not a version Home Assistant loads: ${1-}" >&2
  exit 1
fi

DIR=custom_components/espsomfy_rts

# Newer is a higher X.Y.Z, or the same X.Y.Z replacing a pre-release.
if [[ -n "$IF_NEWER" ]]; then
  current="$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$DIR/manifest.json")"
  current_core="${current%%[!0-9.]*}"
  core="${VERSION%%[!0-9.]*}"
  highest="$(printf '%s\n' "$current_core" "$core" | sort -V | tail -n1)"
  if [[ "$highest" != "$core" ]] || [[ "$current_core" == "$core" && "$current" == "$current_core" ]]; then
    echo "$DIR has $current, not setting $VERSION"
    exit 0
  fi
fi

# Each pattern matches whatever version is there now, suffixes included.
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"${VERSION}\"/" "$DIR/manifest.json"
sed -i "s/^VERSION = \"v[^\"]*\"/VERSION = \"v${VERSION}\"/" "$DIR/const.py"

# sed succeeds when nothing matches, so check each file got the version.
# const.py's must start the line, as the sed's does:
# INTEGRATION_VERSION = "v3.0.4" contains it but isn't it.
check() {
  if ! grep -q -- "$2" "$1"; then
    echo "$1 doesn't have $3: its version string changed shape" >&2
    exit 1
  fi
}
version_re="${VERSION//./\\.}"
check "$DIR/manifest.json" "\"version\": \"${version_re}\"" "\"version\": \"${VERSION}\""
check "$DIR/const.py" "^VERSION = \"v${version_re}\"" "VERSION = \"v${VERSION}\""
