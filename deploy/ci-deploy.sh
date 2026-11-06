#!/usr/bin/env bash
# Deploy one commit to this VM. Run by .github/workflows/backend.yml over an IAP
# SSH session, as root:   sudo bash ci-deploy.sh <full 40-character commit sha>
#
# 1. waits for apt (a freshly started VM runs its own updates and holds the lock)
# 2. backs up the SQLite database, keeping the last 10 copies
# 3. fast-forwards the VM's clone to exactly that commit (it must be on origin/main)
# 4. runs deploy/vm-setup.sh (code, dependencies, services restart; DB and .env kept)
# 5. checks the API reports the new commit; if anything fails, puts the previous
#    commit back and says so (the database backup is the safety net for data)
set -euo pipefail

SHA="${1:?usage: ci-deploy.sh <full commit sha>}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "ci-deploy: '$SHA' is not a full commit sha" >&2; exit 2; }

REPO=/home/arulmotorsnazareth/arul-motors
REPO_USER=arulmotorsnazareth
APP=/opt/arul-motors
VOICE_DOMAIN=voice.arulmotors.com
API_DOMAIN=backend.arulmotors.com
FRONTEND_URL=https://crm.arulmotors.com
SHORT="${SHA:0:12}"
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# One deploy at a time.
exec 9>/var/lock/arul-deploy.lock
flock -n 9 || { echo "ci-deploy: another deploy is running" >&2; exit 3; }

as_repo() { sudo -u "$REPO_USER" git -C "$REPO" "$@"; }

wait_for_apt() {
  local waited=0
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/lib/dpkg/lock >/dev/null 2>&1; do
    [ "$waited" -ge 600 ] && { echo "ci-deploy: apt still busy after 10 minutes" >&2; exit 5; }
    [ "$waited" -eq 0 ] && log "apt is busy (VM updates after boot); waiting"
    sleep 10; waited=$((waited + 10))
  done
}

api_version() {
  curl -fsS -m 5 http://127.0.0.1:8001/api/health 2>/dev/null |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true
}

run_setup() {  # $1 = sha to record as the deployed version
  echo "$1" > "$APP/.deployed-sha"
  chown arul:arul "$APP/.deployed-sha"
  bash "$REPO/deploy/vm-setup.sh" "$VOICE_DOMAIN" "$API_DOMAIN" "$FRONTEND_URL" </dev/null
}

healthy() {  # $1 = expected short sha
  for _ in $(seq 1 30); do
    [ "$(api_version)" = "$1" ] && systemctl is-active --quiet arul-api arul-voice && return 0
    sleep 2
  done
  return 1
}

log "deploying $SHA"
wait_for_apt

if [ -f "$APP/arul_motors.db" ]; then
  BACKUP="$APP/arul_motors.db.pre-deploy-$(date +%Y%m%d-%H%M%S)"
  sudo -u arul sqlite3 "$APP/arul_motors.db" ".backup $BACKUP"
  chmod 600 "$BACKUP"
  log "database backed up to $BACKUP"
  ls -1t "$APP"/arul_motors.db.pre-deploy-* 2>/dev/null | tail -n +11 | xargs -r rm -f
fi

PREV="$(as_repo rev-parse HEAD)"
as_repo fetch --quiet origin main
as_repo cat-file -e "$SHA^{commit}" || { echo "ci-deploy: $SHA is unknown to this clone" >&2; exit 4; }
as_repo merge-base --is-ancestor "$SHA" origin/main || { echo "ci-deploy: $SHA is not on origin/main" >&2; exit 4; }
if [ "$PREV" = "$SHA" ] && [ "$(api_version)" = "$SHORT" ]; then
  log "already running $SHORT; nothing to do"
  exit 0
fi
as_repo merge --ff-only --quiet "$SHA"

if run_setup "$SHA" && healthy "$SHORT"; then
  log "live: $SHORT (was ${PREV:0:12})"
  exit 0
fi

echo "ci-deploy: $SHORT did not come up healthy; rolling back to ${PREV:0:12}" >&2
journalctl -u arul-api -n 30 --no-pager >&2 || true
as_repo reset --hard "$PREV"
run_setup "$PREV" || true
if healthy "${PREV:0:12}"; then
  echo "ci-deploy: rolled back; ${PREV:0:12} is serving again" >&2
else
  echo "ci-deploy: ROLLBACK ALSO UNHEALTHY; check journalctl -u arul-api (database backup is in $APP)" >&2
fi
exit 1
