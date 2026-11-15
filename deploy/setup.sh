#!/usr/bin/env bash
# Turns a fresh Ubuntu 22.04/24.04 server into a running Timesheet. Safe to run again: that is also how you update.
#
#   Clone git@github.com:itej13/Trackr.git to /opt/timesheet first: it's private, so see README "Deploy to a server"
#   for the server's read-only deploy key.
#   sudo /opt/timesheet/deploy/setup.sh                          # address: <server-ip>.sslip.io
#   sudo /opt/timesheet/deploy/setup.sh timesheet.example.com    # your own domain (point its DNS here first)
#
# Update later:  cd /opt/timesheet && sudo git pull && sudo deploy/setup.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR=/var/lib/timesheet
ENV_FILE=/etc/timesheet.env
LLM_MODEL=qwen2.5:14b
PY="$APP_DIR/.venv/bin/python"

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run it with sudo."; exit 1; }
case "$APP_DIR" in /root/* | /home/*) echo "Clone the repo to /opt/timesheet: the service can't read home folders."; exit 1 ;; esac
export DEBIAN_FRONTEND=noninteractive

say "System packages"
apt-get update -q
apt-get install -y -q python3-venv git curl gnupg ufw pciutils unattended-upgrades
if ! command -v caddy >/dev/null; then  # Caddy serves HTTPS and renews the certificate by itself
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

say "Web address"
DOMAIN="${1:-}"
if [ -z "$DOMAIN" ] && [ -f "$ENV_FILE" ]; then  # running again: keep the address chosen last time
  DOMAIN="$(sed -n 's#^TS_PUBLIC_URL=https://##p' "$ENV_FILE")"
fi
if [ -z "$DOMAIN" ]; then  # no domain yet: sslip.io is a free name that points at this server's IP
  IP="$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')"
  case "$IP" in 10.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].*)
    echo "This server's own address ($IP) is private. Run again with a domain that points to its public IP."; exit 1 ;;
  esac
  DOMAIN="${IP//./-}.sslip.io"
fi
echo "https://$DOMAIN"

say "Graphics card"
GPU=no
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  GPU=yes
  nvidia-smi -L
elif lspci | grep -i nvidia >/dev/null; then  # not grep -q: with pipefail an early exit can hide a match
  echo "NVIDIA GPU found but its driver isn't installed. Installing it ..."
  apt-get install -y -q ubuntu-drivers-common && ubuntu-drivers install
  echo "Driver installed. Reboot (sudo reboot), then run this script again."
  exit 0
else
  RAM_GB=$(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo)
  echo "No NVIDIA GPU: both models run on the processor, several times slower than on a GPU."
  [ "$RAM_GB" -ge 24 ] || echo "Warning: ${RAM_GB} GB RAM. The two models need about 16 GB together; 32 GB is comfortable."
fi

say "Python packages"
[ -x "$PY" ] || python3 -m venv "$APP_DIR/.venv"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$APP_DIR/requirements.txt"
CUDA_LIBS=""
if [ "$GPU" = yes ]; then  # the CUDA libraries faster-whisper needs on NVIDIA (per its README)
  "$PY" -m pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
  CUDA_LIBS="$("$PY" -c 'import os, nvidia.cublas.lib as b, nvidia.cudnn.lib as d; print(os.path.dirname(b.__file__) + ":" + os.path.dirname(d.__file__))')"
fi

say "App user, data folder and settings"
id timesheet >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin timesheet
install -d -o timesheet -g timesheet -m 750 "$DATA_DIR"
cat > "$ENV_FILE" <<EOF
# Written by deploy/setup.sh; edits here are replaced when it runs again.
TS_HOST=127.0.0.1
TS_PORT=8000
TS_PUBLIC_URL=https://$DOMAIN
TS_DB=$DATA_DIR/timesheet.db
TS_LLM=$LLM_MODEL
HF_HOME=$DATA_DIR/models
${CUDA_LIBS:+LD_LIBRARY_PATH=$CUDA_LIBS}
EOF
chgrp timesheet "$ENV_FILE" && chmod 640 "$ENV_FILE"

say "Models: $LLM_MODEL (~9 GB) and Whisper large-v3 (~3 GB). The first run downloads them."
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh  # Ollama listens on 127.0.0.1 only
systemctl enable --now ollama >/dev/null
for _ in $(seq 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done
ollama pull "$LLM_MODEL"
runuser -u timesheet -- env HF_HOME="$DATA_DIR/models" "$PY" -c "from faster_whisper import download_model; download_model('large-v3')" >/dev/null

say "Services: app, nightly backup, HTTPS"
cat > /etc/systemd/system/timesheet.service <<EOF
[Unit]
Description=Timesheet web app
After=network-online.target ollama.service
Wants=ollama.service

[Service]
User=timesheet
Group=timesheet
EnvironmentFile=$ENV_FILE
ExecStart=$PY $APP_DIR/app.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/timesheet-backup.service <<EOF
[Unit]
Description=Timesheet database backup (keeps 30 days in $DATA_DIR/backups)

[Service]
Type=oneshot
User=timesheet
EnvironmentFile=$ENV_FILE
ExecStart=$PY $APP_DIR/app.py backup $DATA_DIR/backups
EOF
cat > /etc/systemd/system/timesheet-backup.timer <<EOF
[Unit]
Description=Nightly Timesheet database backup

[Timer]
OnCalendar=*-*-* 02:30:00 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
EOF
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode zstd gzip
	request_body {
		max_size 60MB
	}
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:8000
}
EOF

say "Self-test: loading and running both models once"
systemctl stop timesheet 2>/dev/null || true  # when updating: free the GPU memory the running app holds
SELFTEST=ok
"$APP_DIR/deploy/ts" selftest || SELFTEST=failed

systemctl daemon-reload
systemctl enable timesheet timesheet-backup.timer >/dev/null 2>&1
systemctl restart timesheet
systemctl start timesheet-backup.timer
systemctl reload caddy 2>/dev/null || systemctl restart caddy

say "Firewall: only SSH, HTTP and HTTPS are reachable"
# Allow SSH on whatever port it really uses before switching the firewall on, so this can't lock you out.
for port in $( (sshd -T 2>/dev/null | awk '/^port /{print $2}'; echo 22) | sort -u); do ufw allow "$port/tcp" >/dev/null; done
ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null && ufw status | sed -n '1,12p'

say "Done"
echo "Open:          https://$DOMAIN   (the certificate is fetched on the first visit; allow a minute)"
echo "First admin:   sudo $APP_DIR/deploy/ts adduser admin \"Your Name\" --admin"
echo "Logs:          journalctl -u timesheet -f"
if [ "$SELFTEST" != ok ]; then
  echo "!! The self-test failed (see above). The site works, but recordings won't be transcribed or summarised until it's fixed."
  exit 1
fi
