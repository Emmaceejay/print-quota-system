#!/usr/bin/env bash
#
# Install the printquota CUPS backend and point a queue at it.
#
#   register_backend.sh --install-only      # just (re)install the backend
#   register_backend.sh <queue>             # wrap an existing queue
#   register_backend.sh <queue> --undo      # restore the real device URI
#
# Wrapping is reversible: the real device URI is recorded in the printquota
# database and embedded in the new URI, so --undo needs no extra state.
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/printquota}"
BACKEND_DIR="${BACKEND_DIR:-/usr/lib/cups/backend}"
BACKEND_PATH="$BACKEND_DIR/quota"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run this as root (sudo $0 ...)"
[[ -x "$PREFIX/bin/python" ]] || fail "printquota virtualenv not found at $PREFIX"

install_backend() {
    log "Installing $BACKEND_PATH"
    cat > "$BACKEND_PATH" <<SHIM
#!/bin/sh
# printquota CUPS wrapper backend.
# CUPS runs this as root (mode 0700), so it can read the spool file and
# exec the real backend.
[ -r /etc/printquota/env ] && . /etc/printquota/env && export PRINTQUOTA_CONFIG PRINTQUOTA_SECRET_KEY
exec "$PREFIX/bin/python" -m printquota.backend.quota_backend "\$@"
SHIM
    # 0700 root:root makes CUPS run the backend as root; 0755 would run it
    # as the unprivileged 'lp' user, which cannot read other users' spools.
    chown root:root "$BACKEND_PATH"
    chmod 0700 "$BACKEND_PATH"
}

if [[ "${1:-}" == "--install-only" ]]; then
    install_backend
    exit 0
fi

QUEUE="${1:-}"
[[ -n "$QUEUE" ]] || fail "usage: $0 <queue> [--undo] | --install-only"
command -v lpstat >/dev/null || fail "CUPS client tools are not installed"

CURRENT="$(lpstat -v "$QUEUE" 2>/dev/null | sed -n "s|^device for $QUEUE: ||p")"
[[ -n "$CURRENT" ]] || fail "no such CUPS queue: $QUEUE"

if [[ "${2:-}" == "--undo" ]]; then
    case "$CURRENT" in
        quota:*) REAL="${CURRENT#quota:}" ;;
        *) fail "$QUEUE is not wrapped (device URI: $CURRENT)" ;;
    esac
    log "Restoring $QUEUE -> $REAL"
    lpadmin -p "$QUEUE" -v "$REAL"
    lpstat -v "$QUEUE"
    exit 0
fi

case "$CURRENT" in
    quota:*) log "$QUEUE is already wrapped ($CURRENT)"; exit 0 ;;
esac

install_backend

log "Recording the real device URI in printquota"
"$PREFIX/bin/quotactl" printer add "$QUEUE" --device-uri "$CURRENT" 2>/dev/null || \
    log "printer '$QUEUE' already registered; leaving its cost model alone"

log "Wrapping $QUEUE: $CURRENT -> quota:$CURRENT"
lpadmin -p "$QUEUE" -v "quota:$CURRENT"
lpstat -v "$QUEUE"

cat <<NOTE

$QUEUE now routes through printquota.

Verify with a small test print, then check:
  journalctl -u cups -n 50
  $PREFIX/bin/quotactl usage --days 1

To undo:  sudo $0 $QUEUE --undo
NOTE
