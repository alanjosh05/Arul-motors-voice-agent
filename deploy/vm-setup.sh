#!/usr/bin/env bash
# Install the backend on the VM (Ubuntu 24.04): voice agent + dashboard API +
# SQLite database. Run on the VM, from the folder you copied the code into:
#
#   sudo bash ~/arul-motors/deploy/vm-setup.sh voice.example.com api.example.com https://example.com
#
# Arguments: voice domain, API domain, and the frontend's address on GoDaddy
# (allowed to call the API from the browser; comma-separate several).
# Re-running it updates the code and restarts the services; the database and
# .env are kept.
set -euo pipefail

VOICE_DOMAIN="${1:?Usage: sudo bash vm-setup.sh VOICE_DOMAIN API_DOMAIN FRONTEND_URL}"
API_DOMAIN="${2:?Usage: sudo bash vm-setup.sh VOICE_DOMAIN API_DOMAIN FRONTEND_URL}"
FRONTEND_URL="${3:?Usage: sudo bash vm-setup.sh VOICE_DOMAIN API_DOMAIN FRONTEND_URL}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/arul-motors

apt-get update -q
apt-get install -y -q python3-venv python3-dev build-essential sqlite3 curl \
  debian-keyring debian-archive-keyring apt-transport-https

# Caddy (HTTPS reverse proxy), from its official apt repository.
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

# App user and code. The SQLite database (arul_motors.db) is created next to
# the code on first start and is never overwritten by this script.
id arul >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin arul
mkdir -p "$APP"
cp "$SRC/bot.py" "$SRC/database.py" "$SRC/api.py" "$SRC/auth.py" "$SRC/requirements.txt" "$APP/"
install -m 755 "$SRC/deploy/backup-db.sh" "$APP/backup-db.sh"
if [ ! -f "$APP/.env" ]; then
  cat > "$APP/.env" <<ENV
# Signs CRM staff logins. Changing it signs everyone out.
AUTH_SECRET=$(openssl rand -hex 32)
# Optional key for scripts (X-API-Key header). The CRM uses staff logins instead.
API_KEY=$(openssl rand -hex 24)
# Frontend site(s) allowed to call the API from the browser.
CORS_ORIGINS=$FRONTEND_URL
# Voice agent URL, returned to the frontend by /api/connection.
BOT_URL=https://$VOICE_DOMAIN
# 1 = add the 4 demo customers to the empty database (for a first test call).
SEED_DEMO=0
ENV
  echo ">> Created $APP/.env with a new API_KEY (sudo cat $APP/.env to see it)"
fi
# Servers installed before staff logins existed: add the signing secret once.
if ! grep -q '^AUTH_SECRET=' "$APP/.env"; then
  echo "AUTH_SECRET=$(openssl rand -hex 32)" >> "$APP/.env"
  echo ">> Added AUTH_SECRET to $APP/.env"
fi
chmod 600 "$APP/.env"
chown -R arul:arul "$APP"

sudo -u arul python3 -m venv "$APP/.venv"
sudo -u arul "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u arul "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# systemd services: voice agent and API, restarted on crash and at boot.
sed "s#VOICE_DOMAIN#$VOICE_DOMAIN#g" "$SRC/deploy/arul-voice.service" > /etc/systemd/system/arul-voice.service
cp "$SRC/deploy/arul-api.service" /etc/systemd/system/arul-api.service
systemctl daemon-reload
systemctl enable arul-voice arul-api
systemctl restart arul-voice arul-api

# Nightly database copy to Cloud Storage (20:50 IST, before the 21:00 stop).
cp "$SRC/deploy/arul-backup.service" "$SRC/deploy/arul-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now arul-backup.timer

# Caddy: HTTPS for both domains, basic auth on the voice page.
if ! grep -q "$VOICE_DOMAIN" /etc/caddy/Caddyfile 2>/dev/null; then
  read -rp "Username for the voice page: " VOICE_USER
  HASH="$(caddy hash-password)"   # prompts for the password
  sed -e "s#VOICE_DOMAIN#$VOICE_DOMAIN#" -e "s#API_DOMAIN#$API_DOMAIN#" \
      -e "s#VOICE_USER#$VOICE_USER#" -e "s#VOICE_PASSWORD_HASH#$HASH#" \
    "$SRC/deploy/Caddyfile" > /etc/caddy/Caddyfile
fi
systemctl reload caddy || systemctl restart caddy

echo
echo "Done."
echo "  Voice page: https://$VOICE_DOMAIN/client"
echo "  API:        https://$API_DOMAIN/api/health"
echo "  CRM login:  create the first admin (once):"
echo "              cd $APP && sudo -u arul .venv/bin/python api.py create-user admin --role admin --name \"Your Name\""
echo "  Logs:       journalctl -u arul-voice -f   /   journalctl -u arul-api -f"
echo "  Backups:    nightly 20:50 IST to gs://arulmotors-db-backups (test now: sudo systemctl start arul-backup && journalctl -u arul-backup -n 5)"
