#!/usr/bin/env bash
#
# printquota installer for Ubuntu Server (CUPS host).
#
# Idempotent: safe to re-run for upgrades. It installs the package into a
# dedicated virtualenv, lays out /etc and /var paths, applies migrations and
# installs the systemd units. It does NOT rewrite any CUPS queue -- use
# scripts/register_backend.sh for that, one queue at a time.
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/printquota}"
CONFIG_DIR="${CONFIG_DIR:-/etc/printquota}"
STATE_DIR="${STATE_DIR:-/var/lib/printquota}"
LOG_USER="${LOG_USER:-printquota}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run this as root (sudo $0)"

log "Checking the Python interpreter"
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || \
    fail "Python 3.10 or newer is required; this host has $PY_VER"
log "Using Python $PY_VER"

log "Installing OS dependencies"
export DEBIAN_FRONTEND=noninteractive
OS_PACKAGES=(python3 python3-venv python3-dev build-essential poppler-utils cups libsystemd-dev pkg-config)
MISSING=()
for pkg in "${OS_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
    log "All OS packages are already installed"
else
    # A mirror mid-sync or an unrelated broken third-party repository makes
    # 'apt-get update' fail; that must not stop an upgrade. Only a failure to
    # install a package we actually need is fatal.
    apt-get update -qq || log "WARNING: 'apt-get update' reported errors; continuing with the existing package lists"
    apt-get install -y --no-install-recommends "${MISSING[@]}" >/dev/null || \
        fail "could not install: ${MISSING[*]} (check your internet connection / apt sources and re-run)"
fi

log "Creating the service account and directories"
id -u "$LOG_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$LOG_USER"
install -d -m 0750 -o "$LOG_USER" -g "$LOG_USER" "$STATE_DIR"
install -d -m 0750 -o root -g "$LOG_USER" "$CONFIG_DIR"
# The accounting daemon must be able to read CUPS' page_log.
usermod -a -G lp "$LOG_USER" || true
# The web console manages CUPS queues (enforce / release / add). CUPS lets
# members of its SystemGroup do that over the local socket; on Ubuntu that
# group is lpadmin. CUPS checks /etc/group, so the unit's
# SupplementaryGroups alone is not enough.
getent group lpadmin >/dev/null || groupadd --system lpadmin
usermod -a -G lpadmin "$LOG_USER" || true

log "Building the virtualenv at $PREFIX"
python3 -m venv "$PREFIX"
"$PREFIX/bin/pip" install --quiet --upgrade pip wheel
"$PREFIX/bin/pip" install --quiet "$REPO_DIR"
# Optional: journald logging handler
"$PREFIX/bin/pip" install --quiet systemd-python || \
    log "systemd-python unavailable; logs will go to stderr (still captured by journald)"

if [[ ! -f "$CONFIG_DIR/settings.yaml" ]]; then
    log "Installing default configuration"
    install -m 0640 -o root -g "$LOG_USER" "$REPO_DIR/config/settings.yaml" "$CONFIG_DIR/settings.yaml"
    sed -i "s|url: .*|url: \"sqlite:///$STATE_DIR/printquota.db\"|" "$CONFIG_DIR/settings.yaml"
else
    log "Keeping the existing $CONFIG_DIR/settings.yaml"
fi

if [[ ! -f "$CONFIG_DIR/env" ]]; then
    log "Generating $CONFIG_DIR/env with a fresh session secret"
    umask 077
    cat > "$CONFIG_DIR/env" <<ENVEOF
PRINTQUOTA_CONFIG=$CONFIG_DIR/settings.yaml
PRINTQUOTA_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
ENVEOF
    chown root:"$LOG_USER" "$CONFIG_DIR/env"
    chmod 0640 "$CONFIG_DIR/env"
fi
if ! grep -q '^PRINTQUOTA_SETUP_TOKEN=' "$CONFIG_DIR/env"; then
    log "Generating a one-time setup token for the web console"
    echo "PRINTQUOTA_SETUP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$CONFIG_DIR/env"
fi
SETUP_TOKEN="$(sed -n 's/^PRINTQUOTA_SETUP_TOKEN=//p' "$CONFIG_DIR/env" | tail -n 1)"

log "Applying database migrations"
install -d -m 0755 "$PREFIX/share" "$PREFIX/share/scripts" "$PREFIX/share/docs"
cp -r "$REPO_DIR/alembic.ini" "$PREFIX/share/alembic.ini"
install -m 0755 "$REPO_DIR/scripts/backup.sh" "$PREFIX/share/scripts/backup.sh"
install -m 0644 "$REPO_DIR"/docs/*.md "$PREFIX/share/docs/"
# Browsable HTML copy of the docs (open docs/html/setup.html in a browser).
if [[ -d "$REPO_DIR/docs/html" ]]; then
    install -d -m 0755 "$PREFIX/share/docs/html"
    install -m 0644 "$REPO_DIR"/docs/html/*.html "$PREFIX/share/docs/html/"
fi
( cd "$REPO_DIR" && PRINTQUOTA_CONFIG="$CONFIG_DIR/settings.yaml" "$PREFIX/bin/alembic" upgrade head )
chown "$LOG_USER":"$LOG_USER" "$STATE_DIR"/printquota.db* 2>/dev/null || true

log "Installing the CUPS wrapper backend"
"$REPO_DIR/scripts/register_backend.sh" --install-only

log "Installing systemd units"
install -m 0644 "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
# enable = start at every boot; --now = start immediately as well
systemctl enable --now quota-accounting.service quota-api.service
systemctl enable --now quota-reset.timer quota-backup.timer
# restart (not just start) so an upgrade picks up new code and group membership
systemctl restart quota-accounting.service quota-api.service

SERVER_IP="$(hostname -I | awk '{print $1}')"
cat <<SUMMARY

printquota is installed.

  Web console : http://$SERVER_IP:8080
  Config      : $CONFIG_DIR/settings.yaml
  Database    : $STATE_DIR/printquota.db

Next steps (everything else is done in the browser):

  1. Open the setup page and create your administrator account:

       http://$SERVER_IP:8080/setup?token=$SETUP_TOKEN

     The link works only until the first administrator exists. The token
     is also stored as PRINTQUOTA_SETUP_TOKEN in $CONFIG_DIR/env.

  2. Follow the "Getting started" checklist on the dashboard: groups,
     users (single or CSV import), printers & quota enforcement, alerts.

  3. Require authenticated users in /etc/cups/cupsd.conf so the user name
     on a job cannot be spoofed (README section 20).

SUMMARY
