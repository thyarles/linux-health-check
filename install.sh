#!/usr/bin/env bash
#
# One-call installer for linux-health-check on hosts with no usable system
# python3 (RHEL 7 and friends). Installs a private Miniconda under /root,
# clones the repo, seeds the config, and registers the cron entry.
#
#   curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash
#
# Re-runnable: an existing Miniconda or checkout is reused/updated, and an
# existing healthcheck.conf is never overwritten (it holds your SMTP secrets).
#
# Overridable via env, e.g.  MAIL_DOMAIN=example.org CRON_TIME=06:30 bash install.sh
set -euo pipefail

MINICONDA_URL="${MINICONDA_URL:-https://repo.anaconda.com/miniconda/Miniconda3-py312_25.1.1-0-Linux-x86_64.sh}"
CONDA_PREFIX_DIR="${CONDA_PREFIX_DIR:-/root/miniconda3}"
REPO_URL="${REPO_URL:-https://github.com/thyarles/linux-health-check}"
REPO_BRANCH="${REPO_BRANCH:-main}"
APP_DIR="${APP_DIR:-/root/linux-health-check}"
MAIL_DOMAIN="${MAIL_DOMAIN:-mpt.mp.br}"
CRON_TIME="${CRON_TIME:-}"

PY="$CONDA_PREFIX_DIR/bin/python"
say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (paths under /root)."

# Pick a downloader once; RHEL 7 minimal images have one or the other.
if   command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -q -O "$2" "$1"; }
else die "neither curl nor wget is available."
fi

# ---------------------------------------------------------------- 1. Miniconda
if [ -x "$PY" ]; then
    say "Miniconda already present at $CONDA_PREFIX_DIR — skipping install."
else
    say "Downloading Miniconda"
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    fetch "$MINICONDA_URL" "$tmp/miniconda.sh" || die "download failed: $MINICONDA_URL"
    say "Installing Miniconda to $CONDA_PREFIX_DIR"
    bash "$tmp/miniconda.sh" -b -p "$CONDA_PREFIX_DIR" >/dev/null
    [ -x "$PY" ] || die "installer finished but $PY is missing."
fi
# Fail loudly here rather than from cron at 07:00 if glibc is too old for it.
"$PY" -V >/dev/null 2>&1 || die "$PY will not execute (glibc too old for this Miniconda build?)"
say "Interpreter: $PY ($("$PY" -V 2>&1))"

# --------------------------------------------------------------- 2. Get source
if [ -d "$APP_DIR/.git" ]; then
    say "Updating existing checkout at $APP_DIR"
    ( cd "$APP_DIR" \
      && git fetch --depth 1 origin "$REPO_BRANCH" \
      && git reset --hard "origin/$REPO_BRANCH" ) >/dev/null \
      || die "update failed; move $APP_DIR aside and re-run."
elif command -v git >/dev/null 2>&1; then
    say "Cloning $REPO_URL"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR" >/dev/null \
      || die "clone failed (old git/TLS on this host? unset it and the tarball path will be used)."
else
    # RHEL 7 minimal often has no git, and its 1.8.3 can choke on GitHub's TLS.
    say "git not available — fetching tarball instead"
    tmp2="$(mktemp -d)"
    fetch "$REPO_URL/archive/refs/heads/$REPO_BRANCH.tar.gz" "$tmp2/src.tgz" \
      || { rm -rf "$tmp2"; die "tarball download failed."; }
    mkdir -p "$APP_DIR"
    tar -xzf "$tmp2/src.tgz" -C "$APP_DIR" --strip-components=1
    rm -rf "$tmp2"
fi
[ -f "$APP_DIR/healthcheck.py" ] || die "$APP_DIR/healthcheck.py not found after fetch."

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

say "Done. Review $CONF (SMTP host, recipients), then preview with:"
printf '    %s %s/healthcheck.py text\n\n' "$PY" "$APP_DIR"
