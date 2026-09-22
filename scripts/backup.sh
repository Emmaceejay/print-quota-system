#!/usr/bin/env bash
#
# Consistent backup of the printquota datastore.
# SQLite is copied with the online backup API (safe while services run);
# Postgres is dumped with pg_dump. Keeps the last N copies.
#
set -euo pipefail

CONFIG="${PRINTQUOTA_CONFIG:-/etc/printquota/settings.yaml}"
DEST="${1:-/var/backups/printquota}"
KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"
URL="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
print((yaml.safe_load(open(sys.argv[1])) or {}).get("database", {}).get("url", ""))
PY
)"
[[ -n "$URL" ]] || { echo "could not read database.url from $CONFIG" >&2; exit 1; }

case "$URL" in
  sqlite*)
    DB="${URL#sqlite:///}"
    OUT="$DEST/printquota-$STAMP.db"
    python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
src.close(); dst.close()
PY
    gzip -f "$OUT"
    echo "wrote $OUT.gz"
    ;;
  postgres*|postgresql*)
    OUT="$DEST/printquota-$STAMP.sql.gz"
    pg_dump "$URL" | gzip > "$OUT"
    echo "wrote $OUT"
    ;;
  *)
    echo "unsupported database URL: $URL" >&2; exit 1 ;;
esac

ls -1t "$DEST"/printquota-* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
