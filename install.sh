#!/usr/bin/env bash
#
# One-call installer for linux-health-check on hosts with no usable system
# python3 (RHEL 7 and friends). Installs a private Miniconda under /root,
# downloads a tagged release archive, seeds the config, and registers cron.
#
#   # latest tag
#   curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash
#
#   # a specific tag
#   curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash -s -- v2.0.1
#
# Needs no git: the source arrives as a release tarball. Re-runnable — your
# healthcheck.conf, state/ and reports/ are preserved across upgrades.
#
# Env overrides: REPO_TAG REPO_SLUG APP_DIR CONDA_PREFIX_DIR MAIL_DOMAIN CRON_TIME
set -euo pipefail

MINICONDA_URL="${MINICONDA_URL:-https://repo.anaconda.com/miniconda/Miniconda3-py312_25.1.1-0-Linux-x86_64.sh}"
CONDA_PREFIX_DIR="${CONDA_PREFIX_DIR:-/root/miniconda3}"
REPO_SLUG="${REPO_SLUG:-thyarles/linux-health-check}"
REPO_TAG="${REPO_TAG:-${1:-}}"          # empty or "latest" => newest tag
APP_DIR="${APP_DIR:-/root/linux-health-check}"
MAIL_DOMAIN="${MAIL_DOMAIN:-mpt.mp.br}"
CRON_TIME="${CRON_TIME:-}"

PY="$CONDA_PREFIX_DIR/bin/python"
RAW_URL="https://raw.githubusercontent.com/$REPO_SLUG/main/install.sh"
say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    -h|--help) sed -n '2,18p' "$0" | sed 's/^#\ \?//'; exit 0 ;;
esac
[ "$(id -u)" -eq 0 ] || die "must run as root (paths under /root)."

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- 1. Miniconda
# Only this step needs a system downloader; everything after it uses $PY, whose
# bundled OpenSSL is far newer than RHEL 7's and speaks to GitHub reliably.
if   command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -q -O "$2" "$1"; }
else die "neither curl nor wget is available."
fi

if [ -x "$PY" ]; then
    say "Miniconda already present at $CONDA_PREFIX_DIR — skipping install."
else
    say "Downloading Miniconda"
    fetch "$MINICONDA_URL" "$TMP/miniconda.sh" || die "download failed: $MINICONDA_URL"
    say "Installing Miniconda to $CONDA_PREFIX_DIR"
    bash "$TMP/miniconda.sh" -b -p "$CONDA_PREFIX_DIR" >/dev/null
    [ -x "$PY" ] || die "installer finished but $PY is missing."
fi
# Fail loudly here rather than from cron at 07:00 if glibc is too old for it.
"$PY" -V >/dev/null 2>&1 || die "$PY will not execute (glibc too old for this Miniconda build?)"
say "Interpreter: $PY ($("$PY" -V 2>&1))"

# ------------------------------------------------- helper: GitHub over urllib
cat > "$TMP/gh.py" <<'PY_EOF'
"""Tag resolution and downloads via the interpreter we just installed."""
import re, sys, json, urllib.request

UA = {"User-Agent": "linux-health-check-installer"}  # GitHub 403s without one


def get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def semver(tag):
    """Sort key for a tag; None when it is not a version tag.

    Compares numerically so v2.0.10 outranks v2.0.9, which a lexicographic
    sort gets backwards. A release outranks any prerelease of the same version.
    """
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+](.+))?$", tag.strip())
    if not m:
        return None
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre is None else -1, pre or "")


def latest_tag(slug):
    # Prefer the published release when there is one; it is the maintainer's
    # explicit "latest" and already excludes prereleases.
    try:
        with get("https://api.github.com/repos/%s/releases/latest" % slug) as r:
            tag = json.load(r).get("tag_name")
            if tag:
                return tag
    except Exception:
        pass  # no releases published, or rate-limited: fall back to raw tags

    with get("https://api.github.com/repos/%s/tags?per_page=100" % slug) as r:
        tags = [t["name"] for t in json.load(r)]
    ranked = sorted(((semver(t), t) for t in tags if semver(t)), reverse=True)
    if not ranked:
        raise SystemExit("no version tags found for %s" % slug)
    return ranked[0][1]


cmd = sys.argv[1]
if cmd == "latest-tag":
    print(latest_tag(sys.argv[2]))
elif cmd == "download":
    with get(sys.argv[2]) as r, open(sys.argv[3], "wb") as fh:
        fh.write(r.read())
PY_EOF

# --------------------------------------------------------------- 2. Get source
if [ -z "$REPO_TAG" ] || [ "$REPO_TAG" = "latest" ]; then
    say "Resolving latest tag for $REPO_SLUG"
    REPO_TAG="$("$PY" "$TMP/gh.py" latest-tag "$REPO_SLUG")" \
        || die "could not resolve the latest tag (GitHub unreachable or rate-limited).
     Pass one explicitly:  ... | bash -s -- v2.0.1"
fi
say "Installing $REPO_SLUG @ $REPO_TAG"

# tar.gz rather than .zip: tar is present on even a minimal RHEL 7, unzip often
# is not. Same archive contents either way.
TARBALL="https://github.com/$REPO_SLUG/archive/refs/tags/$REPO_TAG.tar.gz"
"$PY" "$TMP/gh.py" download "$TARBALL" "$TMP/src.tgz" \
    || die "download failed for tag '$REPO_TAG'. Does it exist?
     $TARBALL"

mkdir -p "$TMP/x"
tar -xzf "$TMP/src.tgz" -C "$TMP/x" --strip-components=1 || die "archive did not unpack."
[ -f "$TMP/x/healthcheck.py" ] || die "archive has no healthcheck.py — wrong tag or repo?"

# Upgrade in place. healthcheck.conf, state/ and reports/ live inside APP_DIR and
# are gitignored, so they are absent from the archive and survive untouched:
# the config holds SMTP secrets, and state/ holds the change-detection baselines
# the daily diff depends on. hc/ is purged first so modules deleted upstream do
# not linger and get imported.
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/hc"
cp -a "$TMP/x/." "$APP_DIR/"
printf '%s\n' "$REPO_TAG" > "$APP_DIR/.installed-version"
[ -d "$APP_DIR/.git" ] && say "Note: $APP_DIR/.git is left over from a git install and is no longer used."

# ------------------------------------------------------------------ 3. Config
CONF="$APP_DIR/healthcheck.conf"
if [ -f "$CONF" ]; then
    say "Config already exists — left untouched: $CONF"
else
    say "Creating config with domain.com -> $MAIL_DOMAIN"
    sed "s/domain\.com/$MAIL_DOMAIN/g" "$APP_DIR/healthcheck.conf.example" > "$CONF"
    chmod 600 "$CONF"   # it will hold SMTP credentials
    grep -n "$MAIL_DOMAIN" "$CONF" | sed 's/^/    /'
fi

# ------------------------------------------------------------------ 4. Crontab
say "Installing cron entry"
"$PY" "$APP_DIR/healthcheck.py" crontab ${CRON_TIME:+"$CRON_TIME"}

say "Verifying the installed cron entry"
entry="$(crontab -l 2>/dev/null | grep -F 'linux-healthcheck-managed' || true)"
[ -n "$entry" ] || die "no managed cron entry found after install."
case "$entry" in
    *"$PY"*) printf '    OK: %s\n' "$entry" ;;
    *) printf '    %s\n' "$entry" >&2
       die "cron entry above does not use $PY, so the nightly job would run under
     the wrong interpreter. Tags before v2.0.1 hardcode /usr/bin/python3 and
     cannot honour a private interpreter — re-run on v2.0.1 or later:
       curl -fsSL $RAW_URL | bash
     The cron entry is wrong until you do; 'crontab -e' to remove it by hand." ;;
esac

say "Installed $REPO_TAG. Review $CONF (SMTP host, recipients), then preview with:"
printf '    %s %s/healthcheck.py text\n\n' "$PY" "$APP_DIR"
