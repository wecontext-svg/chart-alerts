#!/usr/bin/env bash
# One-shot server provisioning for chart-alerts. Run as root on a fresh
# Ubuntu 24.04 Hetzner box AFTER copying the chart-alerts folder to
# /opt/chart-alerts. Installs deps, a locked-down service user, the venv,
# the systemd service, and a firewall. Access layer (Tailscale/Caddy) is
# set up separately — see DEPLOY.md.
set -euo pipefail

APP=/opt/chart-alerts
[ -f "$APP/server.py" ] || { echo "Copy the app to $APP first (server.py not found)"; exit 1; }

echo "== packages =="
apt-get update -y
apt-get install -y python3 python3-venv ufw

echo "== service user =="
id chart &>/dev/null || useradd --system --home "$APP" --shell /usr/sbin/nologin chart

echo "== venv + deps =="
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"

echo "== permissions =="
chown -R chart:chart "$APP"
[ -f "$APP/.env" ] && chmod 600 "$APP/.env" || echo "  (!) no .env yet — create $APP/.env with your Telegram creds"

echo "== systemd service =="
cp "$APP/deploy/chart-alerts.service" /etc/systemd/system/chart-alerts.service
systemctl daemon-reload
systemctl enable --now chart-alerts
sleep 2
systemctl --no-pager --lines=8 status chart-alerts || true

echo "== firewall (allow SSH + web) =="
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
yes | ufw enable

echo
echo "DONE. App is live on 127.0.0.1:8000 (localhost only)."
echo "Next: set up access — Tailscale (recommended) or Caddy — per DEPLOY.md."
