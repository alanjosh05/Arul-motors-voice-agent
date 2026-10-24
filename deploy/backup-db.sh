#!/usr/bin/env bash
# Nightly off-site copy of the CRM database to Cloud Storage (run by
# arul-backup.timer at 20:50 IST, before the VM's 21:00 stop).
#
# Makes a consistent copy with SQLite's .backup (safe while the services run),
# compresses it and uploads it as a NEW object. The VM's service account may
# only create objects in the bucket: it can't read, overwrite or delete
# backups. The bucket deletes copies after 90 days.
#
# Manual run:   sudo systemctl start arul-backup && journalctl -u arul-backup -n 20
set -euo pipefail

APP=/opt/arul-motors
DB="${DB_FILE:-$APP/arul_motors.db}"
BUCKET="${BACKUP_BUCKET:-arulmotors-db-backups}"
STAMP="$(TZ=Asia/Kolkata date +%Y-%m-%d_%H%M)"
NAME="daily/arul_motors_${STAMP}.db.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sqlite3 "$DB" ".backup '$TMP/arul_motors.db'"
# Refuse to upload a broken copy.
if [ "$(sqlite3 "$TMP/arul_motors.db" 'PRAGMA integrity_check;')" != "ok" ]; then
  echo "Backup copy failed the integrity check; not uploaded." >&2
  exit 1
fi
CUSTOMERS="$(sqlite3 "$TMP/arul_motors.db" 'SELECT count(*) FROM customers;')"
gzip -9 "$TMP/arul_motors.db"

# Access token for the VM's service account, from the metadata server (no keys).
TOKEN="$(curl -sf -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' \
  | sed -E 's/.*"access_token":"([^"]+)".*/\1/')"

curl -sf -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/gzip' \
  --data-binary "@$TMP/arul_motors.db.gz" \
  "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=$NAME" >/dev/null

echo "Uploaded gs://$BUCKET/$NAME ($(du -h "$TMP/arul_motors.db.gz" | cut -f1), $CUSTOMERS customers)"
