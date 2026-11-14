#!/usr/bin/env bash
# Tests .github/set_version.sh on copies of the real files:
#   bash tests/scripts/test_set_version.sh
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIR=custom_components/espsomfy_rts
FILES=("$DIR/manifest.json" "$DIR/const.py")
failures=0

fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

# A fresh copy of the two files in a temp dir under TMP, which becomes the cwd.
TMP="$(mktemp -d)"
trap 'cd / && rm -rf "$TMP"' EXIT
fresh() {
  WORK="$(mktemp -d -p "$TMP")"
  for f in "${FILES[@]}"; do
    mkdir -p "$WORK/$(dirname "$f")"
    cp "$REPO/$f" "$WORK/$f"
  done
  cd "$WORK"
}

run() { bash "$REPO/.github/set_version.sh" "$@" >/dev/null 2>&1; }

has() { grep -qF -- "$2" "$1" || fail "$3: $1 lacks $2"; }

# Both version strings get the new version, and only they change.
for tag in v3.0.4 v3.1.0-beta.1 v4.0.0-rc1 v3.1.0b1; do
  fresh
  v="${tag#v}"
  run "$tag" || fail "$tag: exited non-zero"
  has "$DIR/manifest.json" "\"version\": \"$v\"" "$tag"
  has "$DIR/const.py" "VERSION = \"v$v\"" "$tag"
  for f in "${FILES[@]}"; do
    changed="$(diff --strip-trailing-cr "$REPO/$f" "$f" | grep -c '^>')"
    ((changed <= 1)) || fail "$tag: changed $changed lines of $f"
  done
  # The manifest is still JSON, with the version Home Assistant reads.
  if python3 -c "" 2>/dev/null; then
    got="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$DIR/manifest.json")"
    [[ "$got" == "$v" ]] || fail "$tag: manifest.json's version is $got"
  fi
done

# Running it twice with the same tag still passes its own check.
fresh
run v3.0.4 && run v3.0.4 || fail "a second run with the same tag failed"

# Tags that aren't versions, or that Home Assistant refuses to load, are
# rejected and nothing is written. v.3.0.0 and 3.0.1.beta are the shapes of
# old releases' tags.
for tag in "v1/2" "v3.0;rm" "v3.0&" "" "vx.y" "v3..0" "v.3.0.0" "v3.1.0.beta" \
  "v3.0.1.beta" "v3.0" "v3.0.0.1" "v3.0.0-" "v3.0.0-beta..1" "v3.0.0c1"; do
  fresh
  if run "$tag"; then fail "accepted '$tag'"; fi
  for f in "${FILES[@]}"; do
    diff -q --strip-trailing-cr "$REPO/$f" "$f" >/dev/null || fail "'$tag' changed $f"
  done
done

# --if-newer: "from to result" sets from, then --if-newer to.
for c in "3.0.5 3.0.4 3.0.5" "3.0.5 3.0.5 3.0.5" "3.0.5 3.0.6 3.0.6" "3.0.9 3.1.0 3.1.0" \
  "3.1.0-beta.1 3.1.0 3.1.0" "3.1.0-beta.1 3.1.0-beta.2 3.1.0-beta.2" \
  "3.1.0-beta.1 3.0.9 3.1.0-beta.1" "3.1.0 3.1.0-beta.1 3.1.0" "3.0.10 3.0.9 3.0.10"; do
  read -r from to want <<<"$c"
  fresh
  run "v$from" || fail "--if-newer $c: setting $from failed"
  run --if-newer "v$to" || fail "--if-newer $c: exited non-zero"
  has "$DIR/manifest.json" "\"version\": \"$want\"" "--if-newer $c"
  has "$DIR/const.py" "VERSION = \"v$want\"" "--if-newer $c"
done
fresh
if run --if-newer v3.0; then fail "--if-newer accepted v3.0"; fi

# A file whose version string changed shape fails loudly instead of
# silently keeping the old version. Both with another version and with the
# one the files already have, which the renamed line still contains.
current="$(sed -n 's/^VERSION = "\(v[^"]*\)".*/\1/p' "$REPO/$DIR/const.py")"
[[ -n "$current" ]] || fail "no VERSION line in the repo's const.py"
for tag in v9.9.9 "$current"; do
  fresh
  sed -i 's/^VERSION = /INTEGRATION_VERSION = /' "$DIR/const.py"
  if run "$tag"; then fail "$tag: passed with no VERSION line"; fi
  fresh
  sed -i 's/"version": /"integration_version": /' "$DIR/manifest.json"
  if run "$tag"; then fail "$tag: passed with no manifest version"; fi
done

if ((failures)); then
  echo "$failures failure(s)"
  exit 1
fi
echo "set_version.sh: all passed"
