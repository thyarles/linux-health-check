#!/usr/bin/env bash
#
# One-call installer for linux-health-check on hosts with no usable system
# python3 (RHEL 7 and friends). Installs a private Miniconda under /root,
# downloads a tagged release archive, seeds the config, and registers cron.
#
#   # the pinned default release (see DEFAULT_TAG below)
#   curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash
#
#   # any other tag
#   curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash -s -- v2.0.1
#
# Needs no git and no GitHub API: the source arrives as a release tarball.
# Re-runnable — healthcheck.conf, state/ and reports/ survive upgrades.
#
# Env overrides: REPO_TAG REPO_SLUG APP_DIR CONDA_PREFIX_DIR MAIL_DOMAIN CRON_TIME
#                DOWNLOADER (curl|wget) — force one if the other is broken
set -euo pipefail

MINICONDA_URL="${MINICONDA_URL:-https://repo.anaconda.com/miniconda/Miniconda3-py312_25.1.1-0-Linux-x86_64.sh}"
CONDA_PREFIX_DIR="${CONDA_PREFIX_DIR:-/root/miniconda3}"
REPO_SLUG="${REPO_SLUG:-thyarles/linux-health-check}"
# The release this installer installs by default. Bump it on main when you cut
# a new tag — install.sh is always fetched from main, so this one line is what
# "latest" means. Deliberately pinned rather than resolved from the GitHub API:
# that API allows 60 unauthenticated calls/hour per IP, which a shared office
# NAT exhausts, and the install then fails for everyone behind it.
DEFAULT_TAG="v2.1.0"
REPO_TAG="${REPO_TAG:-${1:-$DEFAULT_TAG}}"
APP_DIR="${APP_DIR:-/root/linux-health-check}"
MAIL_DOMAIN="${MAIL_DOMAIN:-mpt.mp.br}"
CRON_TIME="${CRON_TIME:-}"

PY="$CONDA_PREFIX_DIR/bin/python"
RAW_URL="https://raw.githubusercontent.com/$REPO_SLUG/main/install.sh"
say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    -h|--help) sed -n '2,17p' "$0" | sed 's/^#\ \?//'; printf 'Default tag: %s\n' "$DEFAULT_TAG"; exit 0 ;;
esac
[ "$(id -u)" -eq 0 ] || die "must run as root (paths under /root)."

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- 1. Miniconda
# Old curl builds (RHEL 6, some 7.x) offer only TLS 1.0, which GitHub has
# refused since 2018 — they fail with "curl: (35) Peer reports incompatible or
# unsupported protocol version". curl being *present* therefore does not mean it
# works, so a curl failure falls back to wget rather than aborting the install.
# Force one with DOWNLOADER=curl or DOWNLOADER=wget.
have()   { command -v "$1" >/dev/null 2>&1; }
_curl()  { curl -fsSL "$1" -o "$2"; }
_wget()  { wget -q -O "$2" "$1"; }

case "${DOWNLOADER:-auto}" in
    curl) have curl || die "DOWNLOADER=curl but curl is not installed."
          fetch() { _curl "$1" "$2"; } ;;
    wget) have wget || die "DOWNLOADER=wget but wget is not installed."
          fetch() { _wget "$1" "$2"; } ;;
    auto)
        if have curl && have wget; then
            fetch() {
                _curl "$1" "$2" && return 0
                printf '    curl could not fetch it (old TLS?) — retrying with wget\n' >&2
                _wget "$1" "$2"
            }
        elif have curl; then fetch() { _curl "$1" "$2"; }
        elif have wget; then fetch() { _wget "$1" "$2"; }
        else die "neither curl nor wget is available."
        fi ;;
    *) die "DOWNLOADER must be 'curl', 'wget', or unset." ;;
esac

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

# --------------------------------------------------------------- 2. Get source
say "Installing $REPO_SLUG @ $REPO_TAG"

# tar.gz rather than .zip: tar is present on even a minimal RHEL 7, unzip often
# is not. Same archive contents either way.
TARBALL="https://github.com/$REPO_SLUG/archive/refs/tags/$REPO_TAG.tar.gz"
fetch "$TARBALL" "$TMP/src.tgz" \
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
