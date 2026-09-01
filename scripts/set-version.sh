#!/usr/bin/env bash
# Set the release version everywhere it is recorded.
#
# The version used to be copied by hand into four files, which is why
# pyproject.toml and uv.lock had already drifted apart. This script is the
# single way to move it — the release workflow runs it, and so can you:
#
#   scripts/set-version.sh 2.1.0
#
# tests/test_version_consistency.py fails the build if the files ever disagree
# again, so a hand-edit to one of them cannot go unnoticed.
set -euo pipefail

VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: $0 X.Y.Z" >&2; exit 1; }
case "$VERSION" in
    v*) echo "pass the bare version, without the leading 'v'" >&2; exit 1 ;;
esac
printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || { echo "not a X.Y.Z version: $VERSION" >&2; exit 1; }

cd "$(dirname "$0")/.."

# The canonical one: the report header reads it at runtime.
sed -i -E "s/^VERSION     = \".*\"/VERSION     = \"$VERSION\"/" hc/utils.py

# Anchored to the [project] table's own key so the dependency versions further
# down the file are never touched.
sed -i -E "0,/^version = \".*\"/s//version = \"$VERSION\"/" pyproject.toml

# What a bare `curl ... | bash` installs. install.sh is always fetched from
# main, so this line is what "current release" means to every server.
sed -i -E "s/^DEFAULT_TAG=\"v.*\"/DEFAULT_TAG=\"v$VERSION\"/" install.sh

# uv records the project version in the lock file too.
if command -v uv >/dev/null 2>&1; then
    uv lock --quiet
else
    echo "warning: uv not found — uv.lock not refreshed" >&2
fi

printf 'version set to %s\n' "$VERSION"
grep -n "^VERSION" hc/utils.py
grep -n "^version" pyproject.toml | head -1
grep -n "^DEFAULT_TAG" install.sh
